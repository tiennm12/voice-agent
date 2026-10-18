// Bridges an eTelecom extension (SIP over WebSocket + WebRTC, via SIP.js) into the
// same /ws pipeline the mic page uses: the caller's audio replaces the microphone,
// and the agent's audio goes into the phone call instead of the speakers.
//
// ponytail: one tab = one extension = one call at a time. To scale past that,
// terminate SIP/WebRTC on the server instead of in a browser tab.
import {
  Registerer,
  RegistererState,
  SessionState,
  UserAgent,
  Web,
} from './vendor/sip-0.20.0.js';

const SIP_SERVER = 'wss://sip.etelecom.vn:10443';

let userAgent;
// Carries the agent's voice into the call. Created on the login click, since an
// AudioContext only starts after a user gesture.
let bridgeContext;
let callOutput; // MediaStreamAudioDestinationNode of the current call
let activeCall = null;

function setSipStatus(text) {
  document.getElementById('sipStatus').textContent = text;
}

async function login(event) {
  event.preventDefault();
  const extension = document.getElementById('extension').value.trim();
  const password = document.getElementById('password').value;
  const domain = document.getElementById('domain').value.trim();

  try {
    bridgeContext = bridgeContext || new AudioContext();
    await bridgeContext.resume();
    if (userAgent) await userAgent.stop();

    userAgent = new UserAgent({
      uri: UserAgent.makeURI(`sip:${extension}@${domain}`),
      authorizationUsername: extension,
      authorizationPassword: password,
      displayName: extension,
      transportOptions: { server: SIP_SERVER },
      // The agent's voice is our "microphone": no getUserMedia, no mic permission.
      sessionDescriptionHandlerFactory: Web.defaultSessionDescriptionHandlerFactory(
        () => Promise.resolve(callOutput.stream),
      ),
      delegate: { onInvite: answer },
    });
    const registerer = new Registerer(userAgent);
    registerer.stateChange.addListener((state) => {
      setSipStatus(state === RegistererState.Registered ? `đã đăng ký ${extension}@${domain}` : state);
    });
    setSipStatus('đang kết nối…');
    await userAgent.start();
    await registerer.register();
  } catch (err) {
    setSipStatus(`lỗi: ${err.message}`);
  }
}

// Whether the owner blocked this number. Any failure counts as "not blocked":
// better to answer a spammer than to turn a real caller away.
async function isBlocked(caller) {
  try {
    const response = await fetch('api/blocked', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ caller }),
    });
    return response.ok && (await response.json()).blocked;
  } catch {
    return false;
  }
}

async function answer(invitation) {
  if (activeCall) {
    invitation.reject(); // busy: the agent handles one call at a time
    return;
  }
  activeCall = invitation;
  if (await isBlocked(invitation.remoteIdentity.uri.user)) {
    activeCall = null;
    invitation.reject(); // never picked up: costs nothing, and the server has logged the attempt
    return;
  }
  // A fresh destination per call: SIP.js stops its tracks when the call ends.
  callOutput = bridgeContext.createMediaStreamDestination();

  invitation.stateChange.addListener((state) => {
    if (state === SessionState.Established) connectAgent(invitation);
    if (state === SessionState.Terminated) {
      activeCall = null;
      stopCall(); // caller hung up (or the call never connected)
    }
  });

  try {
    await invitation.accept();
  } catch (err) {
    activeCall = null;
    appendTranscript('error', `Không nhấc máy được: ${err.message}`);
  }
}

function connectAgent(invitation) {
  const callerAudio = invitation.sessionDescriptionHandler.remoteMediaStream;
  // Chrome only feeds a WebRTC stream into Web Audio while it is also attached to a
  // media element — hence the muted <audio>.
  document.getElementById('remoteAudio').srcObject = callerAudio;

  startCall({
    inputStream: callerAudio,
    meta: {
      caller: invitation.remoteIdentity.uri.user,
      pbxSessionId: invitation.request.getHeader('X-Session-Id'), // matches eTelecom's call log
    },
    audio: 'pcm',
    player: createCallPlayer(bridgeContext, callOutput),
    // The agent hung up (or the server went away): end the phone call too.
    onEnded: () => {
      if (invitation.state === SessionState.Established) invitation.bye();
    },
  });
}

document.getElementById('loginForm').addEventListener('submit', login);
// Lets the audio path be exercised from the console without a real phone call.
window.createCallPlayer = createCallPlayer;
