// The sign-in page (#280). The Firebase Auth SDK is the vendored copy served
// beside this file (vendor/firebase/README.md); nothing loads from another
// origin. Persistence is in memory and there is no popup/redirect resolver, so
// the SDK never loads Google's gapi script: the browser keeps no Firebase state
// after the page closes, and the server's session cookie is the sign-in.
import { initializeApp } from "./vendor/firebase/firebase-app.js";
import {
  initializeAuth,
  inMemoryPersistence,
  signInWithEmailAndPassword,
  signOut,
} from "./vendor/firebase/firebase-auth.js";

const root = document.getElementById("signin");
const form = document.getElementById("signin-form");
const status = document.getElementById("signin-status");
const signedIn = document.getElementById("signed-in");

const auth = initializeAuth(
  initializeApp({
    apiKey: root.dataset.apiKey,
    authDomain: root.dataset.authDomain,
    projectId: root.dataset.projectId,
  }),
  { persistence: inMemoryPersistence },
);

const REFUSALS = {
  401: "That sign-in was not accepted. Try again.",
  403: "This account is not a member of this workspace, or its email is not verified.",
  503: "Sign-in can't be checked right now. Try again shortly.",
};

function show(text) {
  status.textContent = text;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  show("Signing in...");
  const data = new FormData(form);
  try {
    const { user } = await signInWithEmailAndPassword(
      auth,
      String(data.get("email")),
      String(data.get("password")),
    );
    const token = await user.getIdToken();
    // The token goes in the Authorization header, never a URL or a body.
    const response = await fetch("/session", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
    });
    await signOut(auth);
    if (!response.ok) {
      show(REFUSALS[response.status] ?? "Sign-in failed.");
      return;
    }
    const session = await response.json();
    form.hidden = true;
    show("");
    document.getElementById("signed-in-email").textContent = session.email;
    signedIn.hidden = false;
  } catch {
    show("That email and password were not accepted.");
  }
});

document.getElementById("sign-out").addEventListener("click", async () => {
  await fetch("/session/logout", { method: "POST", credentials: "same-origin" });
  signedIn.hidden = true;
  form.hidden = false;
  form.reset();
  show("Signed out.");
});
