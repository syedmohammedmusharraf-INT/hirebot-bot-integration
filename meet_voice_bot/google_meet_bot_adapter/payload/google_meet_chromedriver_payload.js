// Google Meet-specific DOM/RTC bridge: roster (join-status) detection +
// fake-mic wiring. Loaded after pako.min.js, protobuf.min.js and
// shared_chromedriver_payload.js (see web_bot_adapter.py's payload
// injection order).
//
// Trimmed port of attendee/bots/google_meet_bot_adapter/
// google_meet_chromedriver_payload.js (~2650 lines). This file keeps only:
//   - WebSocketClient: JSON-only half of the donor's bridge (message type 1).
//     Video/mixed-audio/per-participant-audio message types are dropped --
//     Meet audio capture for this system goes through PulseAudio
//     (docker/entrypoint.sh + meet_voice_bot/livekit_bridge.py), never this
//     websocket.
//   - FetchInterceptor + the `messageTypes` protobuf field-number table +
//     createMessageDecoder: Meet's own internal (undocumented) wire format
//     for the meeting roster, reverse-engineered by the donor project. This
//     is the one part of the file kept byte-for-byte faithful to the donor
//     rather than rewritten, since guessing a field number wrong would
//     silently corrupt roster parsing.
//   - RTCInterceptor, trimmed to only the "collections" data channel
//     listener that feeds live roster updates into UserManager. Track/SDP/
//     ICE console logging, the captions/media-director channels, and the
//     screenshare/per-participant-video track handling are all dropped.
//   - UserManager: roster diffing that emits `UsersUpdate` (join-status,
//     see WebBotAdapter.handle_websocket_message / STATUS_IN_MEETING).
//   - A small poller (was StyleManager.checkNeededInteractions, called every
//     5s) that auto-accepts the "this call is being recorded" dialog and
//     emits `MeetingStatusChange` for removed_from_meeting/meeting_ended.
//   - turnOnMic/turnOffMic (real Meet mic-button togglers) and the
//     BotOutputManager wiring that uses them.
//
// Dropped entirely: captions/transcript capture (CaptionManager and the
// Caption*/ChatMessage* protobuf message types), chat mirroring, per-
// participant/mixed video and audio websocket sending, webcam/screenshare
// output, layout/reactions UI tweaks, and localStorage incoming-video prefs.

// ---------------------------------------------------------------------
// WebSocketClient: JSON-only bridge to the Python side.
// ---------------------------------------------------------------------
class WebSocketClient {
    static MESSAGE_TYPES = { JSON: 1 };

    constructor() {
        this.ws = null;

        // Connecting synchronously here (during Page.addScriptToEvaluate
        // OnNewDocument's document-start execution, i.e. mid-navigation)
        // reliably made the *navigation itself* abort: `driver.get()` would
        // hang 40-70s and Chrome would report `net::ERR_ABORTED` for the
        // Document load, even though `Page.frameNavigated` had already
        // fired with the correct https://meet.google.com origin. Bisected
        // and root-caused via scripts/diag_join.py -- opening this
        // WebSocket was the one thing in the whole injected payload that
        // reproduced it; every other combination (fetch/RTC interceptors,
        // BotOutputManager alone) navigated fine. Deferring the actual
        // connect until the page has finished loading avoids the timing
        // conflict; nothing needs this connection to be live any earlier --
        // join-detection (wait_until_admitted in google_meet_ui_methods.py)
        // polls the DOM directly, not this websocket.
        if (document.readyState === "complete") {
            this._connect();
        } else {
            window.addEventListener("load", () => this._connect(), { once: true });
        }
    }

    _connect() {
        const url = `ws://localhost:${window.initialData.websocketPort}`;
        this.ws = new WebSocket(url);
        this.ws.binaryType = "arraybuffer";

        this.ws.onopen = () => console.log("WebSocket Connected");
        this.ws.onerror = (error) => console.error("WebSocket Error:", error);
        this.ws.onclose = () => console.log("WebSocket Disconnected");
    }

    sendJson(data) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.error("WebSocket is not connected");
            return;
        }
        try {
            const jsonBytes = new TextEncoder().encode(JSON.stringify(data));
            const message = new Uint8Array(4 + jsonBytes.length);
            new DataView(message.buffer).setInt32(0, WebSocketClient.MESSAGE_TYPES.JSON, true);
            message.set(jsonBytes, 4);
            this.ws.send(message.buffer);
        } catch (error) {
            console.error("Error sending WebSocket message:", error, data);
        }
    }
}

// ---------------------------------------------------------------------
// FetchInterceptor / RTCInterceptor
// ---------------------------------------------------------------------
class FetchInterceptor {
    constructor(responseCallback) {
        this.originalFetch = window.fetch;
        this.responseCallback = responseCallback;
        window.fetch = (...args) => this.interceptFetch(...args);
    }

    async interceptFetch(...args) {
        const response = await this.originalFetch.apply(window, args);
        try {
            await this.responseCallback(response.clone());
        } catch (error) {
            console.error("Error in intercepted fetch callback:", error);
        }
        return response;
    }
}

class RTCInterceptor {
    constructor(callbacks) {
        const originalRTCPeerConnection = window.RTCPeerConnection;
        const onPeerConnectionCreate = callbacks.onPeerConnectionCreate || (() => {});

        window.RTCPeerConnection = function (...args) {
            const peerConnection = Reflect.construct(originalRTCPeerConnection, args);
            onPeerConnectionCreate(peerConnection);
            return peerConnection;
        };
    }
}

// ---------------------------------------------------------------------
// Meet's internal roster wire format (verbatim field numbers from the
// donor's reverse-engineering -- see module docstring above).
// ---------------------------------------------------------------------
const messageTypes = [
    {
        name: "CollectionEvent",
        fields: [{ name: "body", fieldNumber: 1, type: "message", messageType: "CollectionEventBody" }],
    },
    {
        name: "CollectionEventBody",
        fields: [{ name: "userInfoListWrapperAndChatWrapperWrapper", fieldNumber: 2, type: "message", messageType: "UserInfoListWrapperAndChatWrapperWrapper" }],
    },
    {
        name: "UserInfoListWrapperAndChatWrapperWrapper",
        fields: [{ name: "userInfoListWrapperAndChatWrapper", fieldNumber: 13, type: "message", messageType: "UserInfoListWrapperAndChatWrapper" }],
    },
    {
        name: "UserInfoListWrapperAndChatWrapper",
        fields: [{ name: "userInfoListWrapper", fieldNumber: 1, type: "message", messageType: "UserInfoListWrapper" }],
    },
    {
        name: "UserInfoListResponse",
        fields: [{ name: "userInfoListWrapperWrapper", fieldNumber: 2, type: "message", messageType: "UserInfoListWrapperWrapper" }],
    },
    {
        name: "UserInfoListWrapperWrapper",
        fields: [{ name: "userInfoListWrapper", fieldNumber: 2, type: "message", messageType: "UserInfoListWrapper" }],
    },
    {
        name: "UserEventInfo",
        fields: [{ name: "eventNumber", fieldNumber: 1, type: "varint" }],
    },
    {
        name: "UserInfoListWrapper",
        fields: [
            { name: "userEventInfo", fieldNumber: 1, type: "message", messageType: "UserEventInfo" },
            { name: "userInfoList", fieldNumber: 2, type: "message", messageType: "UserInfoList", repeated: true },
        ],
    },
    {
        name: "UserInfoList",
        fields: [
            { name: "deviceId", fieldNumber: 1, type: "string" },
            { name: "fullName", fieldNumber: 2, type: "string" },
            { name: "profilePicture", fieldNumber: 3, type: "string" },
            { name: "status", fieldNumber: 4, type: "varint" }, // in meeting = 1, not in meeting = 6, removed = 7
            { name: "isCurrentUserString", fieldNumber: 7, type: "string" }, // presence indicates this is the bot itself
            { name: "displayName", fieldNumber: 29, type: "string" },
            { name: "parentDeviceId", fieldNumber: 21, type: "string" }, // present => this entry is a screenshare device
            { name: "isHost", fieldNumber: 34, type: "varint" },
        ],
    },
];

function createMessageDecoder(messageType) {
    return function decode(reader, length) {
        if (!(reader instanceof protobuf.Reader)) {
            reader = protobuf.Reader.create(reader);
        }
        const end = length === undefined ? reader.len : reader.pos + length;
        const message = {};

        while (reader.pos < end) {
            const tag = reader.uint32();
            const fieldNumber = tag >>> 3;
            const field = messageType.fields.find((f) => f.fieldNumber === fieldNumber);

            if (!field) {
                reader.skipType(tag & 7);
                continue;
            }

            let value;
            switch (field.type) {
                case "string":
                    value = reader.string();
                    break;
                case "varint":
                    value = reader.uint32();
                    break;
                case "int64":
                    value = reader.int64().toNumber();
                    break;
                case "message": {
                    const nestedLength = reader.uint32();
                    const nestedType = messageTypes.find((t) => t.name === field.messageType);
                    value = createMessageDecoder(nestedType)(reader, nestedLength);
                    break;
                }
                default:
                    reader.skipType(tag & 7);
                    continue;
            }

            if (field.repeated) {
                (message[field.name] = message[field.name] || []).push(value);
            } else {
                message[field.name] = value;
            }
        }

        return message;
    };
}

const messageDecoders = {};
messageTypes.forEach((type) => {
    messageDecoders[type.name] = createMessageDecoder(type);
});

function base64ToUint8Array(base64) {
    const binaryString = atob(base64);
    const bytes = new Uint8Array(binaryString.length);
    for (let i = 0; i < binaryString.length; i++) {
        bytes[i] = binaryString.charCodeAt(i);
    }
    return bytes;
}

// ---------------------------------------------------------------------
// UserManager: roster diffing -> `UsersUpdate` events.
// ---------------------------------------------------------------------
class UserManager {
    constructor(ws) {
        this.ws = ws;
        this.allUsersMap = new Map();
        this.currentUsersMap = new Map();
        this.currentUserId = null;
    }

    MEETING_STATUS = { IN_MEETING: 1, NOT_IN_MEETING: 6 };

    getUserByDeviceId(deviceId) {
        return this.allUsersMap.get(deviceId);
    }

    singleUserSynced(user) {
        const allUsers = [...this.currentUsersMap.values(), user];
        const uniqueUsers = Array.from(new Map(allUsers.map((u) => [u.deviceId, u])).values());
        this.newUsersListSynced(uniqueUsers);
    }

    newUsersListSynced(newUsersListRaw) {
        const userStatusMap = { 1: "in_meeting", 6: "not_in_meeting", 7: "removed_from_meeting" };

        const newUsersList = newUsersListRaw.map((user) => {
            const { isCurrentUserString, ...rest } = user;
            if (isCurrentUserString && this.currentUserId === null) {
                this.currentUserId = user.deviceId;
            }
            return {
                ...rest,
                humanized_status: userStatusMap[user.status] || "unknown",
                isCurrentUser: user.deviceId === this.currentUserId,
                isHost: !!user.isHost,
            };
        });

        const previousUserIds = new Set(this.currentUsersMap.keys());
        const newUserIds = new Set(newUsersList.map((u) => u.deviceId));

        for (const user of newUsersList) {
            this.allUsersMap.set(user.deviceId, user);
        }

        const newUsers = newUsersList.filter((u) => !previousUserIds.has(u.deviceId));
        const updatedUsers = newUsersList.filter((u) => previousUserIds.has(u.deviceId) && JSON.stringify(this.currentUsersMap.get(u.deviceId)) !== JSON.stringify(u));
        const removedUsers = Array.from(previousUserIds)
            .filter((id) => !newUserIds.has(id))
            .map((id) => this.currentUsersMap.get(id));

        this.currentUsersMap.clear();
        for (const user of newUsersList) {
            this.currentUsersMap.set(user.deviceId, user);
        }

        if (newUsers.length || updatedUsers.length || removedUsers.length) {
            this.ws.sendJson({
                type: "UsersUpdate",
                newUsers,
                updatedUsers,
                removedUsers,
            });
        }
    }
}

// ---------------------------------------------------------------------
// Recording-notification auto-accept + removed/ended detection, polled
// every 5s (was StyleManager.checkNeededInteractions in the donor).
// ---------------------------------------------------------------------
function checkNeededInteractions(ws) {
    const recordingDialog = document.querySelector('div[aria-modal="true"][role="dialog"], div[aria-modal="true"][role="alertdialog"]');
    if (
        recordingDialog &&
        (recordingDialog.textContent.includes("This video call is being recorded") ||
            recordingDialog.textContent.includes("Others may see your video differently") ||
            recordingDialog.textContent.includes("This video call is being transcribed") ||
            recordingDialog.textContent.includes("Gemini is taking notes") ||
            recordingDialog.textContent.includes("This meeting is being captured"))
    ) {
        const joinNowButton = recordingDialog.querySelector('button[data-mdc-dialog-action="ok"]');
        if (joinNowButton) {
            try {
                joinNowButton.click();
            } catch (error) {
                console.error("Error clicking button to accept recording notification", error);
            }
        }
    }

    const callEndedMessageElement = document.querySelector(".roSPhc");
    if (callEndedMessageElement && callEndedMessageElement.textContent.includes("You've been removed from the meeting")) {
        ws.sendJson({ type: "MeetingStatusChange", change: "removed_from_meeting" });
    }
    if (
        callEndedMessageElement &&
        (callEndedMessageElement.textContent.includes("The call ended because everyone left") || callEndedMessageElement.textContent.includes("Your host ended the meeting for everyone"))
    ) {
        ws.sendJson({ type: "MeetingStatusChange", change: "meeting_ended" });
    }
}

// ---------------------------------------------------------------------
// Real Meet mic-button togglers (join-time state, distinct from
// BotOutputManager.setMicMuted's instant gain-node mute).
// ---------------------------------------------------------------------
function turnOnMic() {
    const button = document.querySelector('button[aria-label="Turn on microphone"]');
    if (button) button.click();
}

function turnOffMic() {
    const button = document.querySelector('button[aria-label="Turn off microphone"]');
    if (button) button.click();
}

// ---------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------
// `Page.addScriptToEvaluateOnNewDocument` evaluates this script on the
// browser's *current* document too, not just future navigations -- which,
// at the point web_bot_adapter.py's init_driver() installs it, is still the
// tab's initial blank `data:,` page. Skipping all of this there costs
// nothing (each document gets its own fresh JS realm on navigation, so it
// runs again, correctly, on the real Meet document) and is cheap insurance,
// but it was NOT the actual root cause of a real, 100%-reproducible bug
// this system hit: `driver.get(meeting_url)` would hang 40-70s and return
// having never left `data:,`, with `net::ERR_ABORTED` for the Document load
// in the performance log even though CDP's own `Page.frameNavigated` had
// already fired with the correct https://meet.google.com origin. Bisected
// with scripts/diag_join.py by disabling pieces of this Wiring block one at
// a time: the trigger was specifically `WebSocketClient` opening its
// `ws://localhost:PORT` connection *synchronously in its constructor* --
// regardless of which document ran it. Every other combination (Fetch/RTC
// interceptors, BotOutputManager alone) navigated in a few seconds; adding
// the live WebSocket connect back in reproduced the hang every time. See
// `WebSocketClient._connect()` below -- it now defers the actual `new
// WebSocket(...)` call until `window.load`, which is enough to avoid
// whatever timing conflict this created with the navigation commit.
if (location.protocol === "http:" || location.protocol === "https:") {
    const ws = new WebSocketClient();
    window.ws = ws;

    const userManager = new UserManager(ws);
    window.userManager = userManager;

    // BotOutputManager is defined in shared_chromedriver_payload.js.
    window.botOutputManager = new BotOutputManager({ turnOnMic, turnOffMic });

    setInterval(() => checkNeededInteractions(ws), 5000);

    // Initial roster snapshot: Meet's SyncMeetingSpaceCollections RPC response.
    const syncMeetingSpaceCollectionsUrl = "https://meet.google.com/$rpc/google.rtc.meetings.v1.MeetingSpaceService/SyncMeetingSpaceCollections";
    new FetchInterceptor(async (response) => {
        if (response.url !== syncMeetingSpaceCollectionsUrl) {
            return;
        }
        const decodedData = base64ToUint8Array(await response.text());
        const userInfoListResponse = messageDecoders["UserInfoListResponse"](decodedData);
        const userInfoList = userInfoListResponse.userInfoListWrapperWrapper?.userInfoListWrapper?.userInfoList || [];
        if (userInfoList.length > 0) {
            userManager.newUsersListSynced(userInfoList);
        }
    });

    // Live roster updates: Meet pushes them over a WebRTC data channel labeled
    // "collections", gzip'd protobuf CollectionEvent frames.
    const handleCollectionEvent = (event) => {
        const decodedData = pako.inflate(new Uint8Array(event.data));
        const collectionEvent = messageDecoders["CollectionEvent"](decodedData);
        // A CollectionEvent frame generally reports a single user (a join or a
        // leave); we can't tell which from this event alone, so it's applied as
        // a "synced" (join-shaped) update. A genuine leave is corrected by the
        // next periodic SyncMeetingSpaceCollections snapshot, at the cost of up
        // to roughly a minute of lag on leave events specifically.
        const userInfoList = collectionEvent.body?.userInfoListWrapperAndChatWrapperWrapper?.userInfoListWrapperAndChatWrapper?.userInfoListWrapper?.userInfoList || [];
        for (const user of userInfoList) {
            userManager.singleUserSynced(user);
        }
    };

    new RTCInterceptor({
        onPeerConnectionCreate: (peerConnection) => {
            peerConnection.addEventListener("datachannel", (event) => {
                if (event.channel.label === "collections") {
                    event.channel.addEventListener("message", handleCollectionEvent);
                }
            });
        },
    });
}
