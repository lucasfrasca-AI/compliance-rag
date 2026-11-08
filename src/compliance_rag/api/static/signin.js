// The sign-in page (#280, #291). The Firebase Auth SDK is the vendored copy served
// beside this file (vendor/firebase/README.md). Persistence is in memory, so the
// browser keeps no Firebase state after the page closes: the server's session
// cookie is the sign-in, and both methods (email and password, Google popup)
// hand their ID token to the same POST /session.
//
// Google sign-in needs the popup resolver, which loads Google's gapi script and
// frames the project's auth domain. The page's CSP allows exactly those two and
// nothing else (sign_in.sign_in_content_security_policy).
import { initializeApp } from "./vendor/firebase/firebase-app.js";
import {
  GoogleAuthProvider,
  browserPopupRedirectResolver,
  initializeAuth,
  inMemoryPersistence,
  signInWithEmailAndPassword,
  signInWithPopup,
  signOut,
} from "./vendor/firebase/firebase-auth.js";

const root = document.getElementById("signin");
const form = document.getElementById("signin-form");
const googleRow = document.getElementById("google-row");
const googleButton = document.getElementById("google-sign-in");
const status = document.getElementById("signin-status");
const signedIn = document.getElementById("signed-in");

const auth = initializeAuth(
  initializeApp({
    apiKey: root.dataset.apiKey,
    authDomain: root.dataset.authDomain,
    projectId: root.dataset.projectId,
  }),
  // The popup resolver is given once, here, and signInWithPopup uses it by default.
  // Here rather than per call because the SDK then starts loading the gapi frame at
  // page load on Safari and mobile browsers, which is what stops them blocking the
  // popup (it must open inside the click, before any awaited load).
  { persistence: inMemoryPersistence, popupRedirectResolver: browserPopupRedirectResolver },
);

const REFUSALS = {
  401: "That sign-in was not accepted. Try again.",
  403: "This account is not a member of this workspace, or its email is not verified.",
  503: "Sign-in can't be checked right now. Try again shortly.",
};

const GOOGLE_FAILURES = {
  "auth/popup-blocked": "The browser blocked the Google window. Allow pop-ups for this page and try again.",
  "auth/popup-closed-by-user": "Google sign-in was closed before it finished.",
  "auth/cancelled-popup-request": "Google sign-in was closed before it finished.",
  "auth/unauthorized-domain": "Google sign-in is not enabled for this address.",
  "auth/operation-not-allowed": "Google sign-in is not enabled for this workspace.",
};

function show(text) {
  status.textContent = text;
}

// Swap the signed-in user's ID token for the session cookie. The token goes in
// the Authorization header, never a URL or a body, and the Firebase user is
// signed out of the SDK straight after, whether or not the hand-over worked (a
// network error included): the cookie is the only thing kept.
async function startSession(user) {
  let response;
  try {
    const token = await user.getIdToken();
    response = await fetch("/session", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
    });
  } catch {
    show(REFUSALS[503]);
    return;
  } finally {
    await signOut(auth).catch(() => {});
  }
  if (!response.ok) {
    show(REFUSALS[response.status] ?? "Sign-in failed.");
    return;
  }
  const session = await response.json();
  form.hidden = true;
  googleRow.hidden = true;
  show("");
  document.getElementById("signed-in-email").textContent = session.email;
  signedIn.hidden = false;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  show("Signing in...");
  const data = new FormData(form);
  let user;
  try {
    ({ user } = await signInWithEmailAndPassword(
      auth,
      String(data.get("email")),
      String(data.get("password")),
    ));
  } catch {
    show("That email and password were not accepted.");
    return;
  }
  await startSession(user);
});

googleButton.addEventListener("click", async () => {
  show("Waiting for Google...");
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: "select_account" });
  let user;
  try {
    ({ user } = await signInWithPopup(auth, provider));
  } catch (error) {
    show(GOOGLE_FAILURES[error?.code] ?? "Google sign-in did not complete.");
    return;
  }
  await startSession(user);
});

document.getElementById("sign-out").addEventListener("click", async () => {
  await fetch("/session/logout", { method: "POST", credentials: "same-origin" });
  signedIn.hidden = true;
  form.hidden = false;
  googleRow.hidden = false;
  form.reset();
  show("Signed out.");
});
