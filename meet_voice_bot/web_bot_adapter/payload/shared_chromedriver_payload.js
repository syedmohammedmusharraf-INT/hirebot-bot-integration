// Fake-microphone shim, shared across web bot adapters.
//
// Trimmed port of attendee/bots/web_bot_adapter/shared_chromedriver_payload.js
// (donor lines ~471-745 for BotOutputManager's audio path). Dropped
// entirely: BotVideoOutputStream, webcam/screenshare output, the bot-output
// WebRTC peer connection (getBotOutputPeerConnectionOffer /
// startBotOutputPeerConnection / playBotOutputMediaStream -- that's the
// forbidden "webpage_streamer" feature), and playVideo/displayImage. This
// bot never sends video, so only the virtual-microphone half of
// BotOutputManager survives.
//
// Loaded (via Page.addScriptToEvaluateOnNewDocument, see web_bot_adapter.py)
// after initialData + the pako/protobuf libraries but before the
// platform-specific payload, so `BotOutputManager` is available when e.g.
// google_meet_chromedriver_payload.js instantiates it.

class BotOutputManager {
    /**
     * @param {Object} callbacks
     * @param {Function} [callbacks.turnOnMic] - click the platform's real mic-on UI button (join-time state).
     * @param {Function} [callbacks.turnOffMic] - click the platform's real mic-off UI button (join-time state).
     */
    constructor({ turnOnMic = () => {}, turnOffMic = () => {} } = {}) {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            throw new Error("navigator.mediaDevices.getUserMedia is not available in this context.");
        }

        this.turnOnMic = turnOnMic;
        this.turnOffMic = turnOffMic;

        // Not created until first needed -- creating it eagerly plays the
        // (silent, empty) source track through the speakers for some reason.
        this.sourceAudioTrack = null;

        // ---- AUDIO QUEUE STATE ----
        this.audioQueue = [];
        this.isPlayingAudioQueue = false;
        this.nextPlayTime = 0;
        this.sampleRate = 44100;
        this.numChannels = 1;
        this.turnOffMicTimeout = null;
        this.micMuted = false;

        this._originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
        this._installGetUserMediaInterceptor();
    }

    _createSourceAudioTrack() {
        if (this.sourceAudioTrack) {
            return;
        }
        this.audioContext = new AudioContext();
        this.gainNode = this.audioContext.createGain();
        this.audioDestination = this.audioContext.createMediaStreamDestination();

        this.gainNode.gain.value = 1.0;
        this.gainNode.connect(this.audioDestination);
        // Intentionally NOT connected to this.audioContext.destination --
        // doing so would play the agent's answer audio out of the bot's own
        // (Pulse) speakers, which the capture side would then pick right
        // back up and re-publish to LiveKit as an echo.

        const audioTracks = this.audioDestination.stream.getAudioTracks();
        this.sourceAudioTrack = audioTracks[0] || null;
    }

    _installGetUserMediaInterceptor() {
        const self = this;
        navigator.mediaDevices.getUserMedia = async function interceptedGetUserMedia(constraints) {
            const needAudio = !!(constraints && constraints.audio !== false && constraints.audio != null);
            const needVideo = !!(constraints && constraints.video !== false && constraints.video != null);

            if (!needAudio && !needVideo) {
                return self._originalGetUserMedia(constraints);
            }

            const stream = new MediaStream();

            if (needAudio) {
                self._createSourceAudioTrack();
                const audioClone = self.sourceAudioTrack.clone();
                stream.addTrack(audioClone);
            }
            // needVideo is intentionally not serviced: this bot never turns
            // its camera on (turn_off_media_inputs keeps it off), so a
            // caller requesting video gets an audio-only stream back.

            return stream;
        };
    }

    ensureMicOn() {
        try {
            this.turnOnMic && this.turnOnMic();
        } catch (e) {
            console.error("Error in turnOnMic callback:", e);
        }
    }

    disableMic() {
        try {
            this.turnOffMic && this.turnOffMic();
        } catch (e) {
            console.error("Error in turnOffMic callback:", e);
        }
    }

    /**
     * Mute/unmute the synthesized mic output instantly (gain node), without
     * touching Meet's own mic UI button. Used for the voice agent's
     * per-utterance input guard (see plan section 4.5) -- much faster than
     * clicking a DOM button, and doesn't fight the join-time turnOffMic().
     */
    setMicMuted(muted) {
        this.micMuted = !!muted;
        if (this.gainNode) {
            this.gainNode.gain.value = this.micMuted ? 0.0 : 1.0;
        }
    }

    /**
     * Play raw PCM audio data through the virtual microphone.
     * @param {Int16Array|Float32Array|Array<number>} pcmData
     * @param {number} [sampleRate=44100]
     * @param {number} [numChannels=1]
     */
    async playPCMAudio(pcmData, sampleRate = 44100, numChannels = 1) {
        this._createSourceAudioTrack();
        this.ensureMicOn();

        if (this.sampleRate !== sampleRate || this.numChannels !== numChannels) {
            this.sampleRate = sampleRate;
            this.numChannels = numChannels;
        }

        let audioData;
        if (pcmData instanceof Float32Array) {
            audioData = pcmData;
        } else {
            audioData = new Float32Array(pcmData.length);
            for (let i = 0; i < pcmData.length; i++) {
                audioData[i] = pcmData[i] / 32768.0;
            }
        }

        const duration = audioData.length / (numChannels * sampleRate);
        this.audioQueue.push({ data: audioData, duration });

        if (this.turnOffMicTimeout) {
            clearTimeout(this.turnOffMicTimeout);
            this.turnOffMicTimeout = null;
        }

        if (!this.isPlayingAudioQueue) {
            this._processAudioQueue();
        }
    }

    _processAudioQueue() {
        if (this.audioQueue.length === 0) {
            this.isPlayingAudioQueue = false;

            if (this.turnOffMicTimeout) {
                clearTimeout(this.turnOffMicTimeout);
            }
            this.turnOffMicTimeout = setTimeout(() => {
                if (this.audioQueue.length === 0) {
                    this.disableMic();
                }
            }, 2000);

            return;
        }

        this.isPlayingAudioQueue = true;

        const currentTime = this.audioContext.currentTime;
        if (!this.nextPlayTime || this.nextPlayTime < currentTime) {
            this.nextPlayTime = currentTime;
        }

        const { data, duration } = this.audioQueue.shift();
        const frames = data.length / this.numChannels;
        const audioBuffer = this.audioContext.createBuffer(this.numChannels, frames, this.sampleRate);

        if (this.numChannels === 1) {
            audioBuffer.getChannelData(0).set(data);
        } else {
            for (let ch = 0; ch < this.numChannels; ch++) {
                const channelData = audioBuffer.getChannelData(ch);
                for (let i = 0; i < frames; i++) {
                    channelData[i] = data[i * this.numChannels + ch];
                }
            }
        }

        const source = this.audioContext.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(this.gainNode); // -> gain node -> mic track

        source.start(this.nextPlayTime);
        this.nextPlayTime += duration;

        const timeUntilNextProcessMs = (this.nextPlayTime - currentTime) * 1000 * 0.8;
        setTimeout(() => this._processAudioQueue(), Math.max(0, timeUntilNextProcessMs));
    }
}
