// Fantasy World Cup — shared Firebase init + auth helpers.
//
// HOW THIS WORKS:
//   - This file loads the Firebase JS SDK from Google's CDN (no build step).
//   - It exposes `auth`, `db`, and helper functions on window.fwc.
//   - Other pages should import this with: <script type="module" src="./shared.js"></script>
//
// BEFORE THIS WILL WORK:
//   1. Create a Firebase project at https://console.firebase.google.com
//   2. Add a web app to the project ("Add app" → web icon).
//   3. In Build → Authentication, enable the "Google" sign-in provider.
//   4. Copy the firebaseConfig object Firebase gives you into the
//      FIREBASE_CONFIG constant below.
//   5. In Build → Firestore Database, create the database in production
//      mode and apply the rules from firestore.rules.

import { initializeApp } from "https://www.gstatic.com/firebasejs/10.13.0/firebase-app.js";
import {
  getAuth,
  GoogleAuthProvider,
  signInWithPopup,
  signOut as fbSignOut,
  onAuthStateChanged,
} from "https://www.gstatic.com/firebasejs/10.13.0/firebase-auth.js";
import {
  getFirestore,
  doc,
  getDoc,
  setDoc,
} from "https://www.gstatic.com/firebasejs/10.13.0/firebase-firestore.js";

// Firebase web config. These values are intentionally PUBLIC — security
// comes from Firestore rules + Auth provider restrictions, NOT from
// keeping these secret. Safe to commit.
const FIREBASE_CONFIG = {
  apiKey: "AIzaSyC5MojM3xkLTeeFjy8yhBKm6In7Zvq6Udk",
  authDomain: "fantasy-world-cup-2026.firebaseapp.com",
  projectId: "fantasy-world-cup-2026",
  storageBucket: "fantasy-world-cup-2026.firebasestorage.app",
  messagingSenderId: "155847257242",
  appId: "1:155847257242:web:a674e2aa741e9705c74323",
};

// Admin uid(s) — these accounts can write to teams, players, matches, config.
// Will be enforced by Firestore security rules. Populate after first sign-in
// (sign in once with the admin Google account, look up the uid in Firebase
// console → Authentication, paste it here AND in firestore.rules).
export const ADMIN_UIDS = new Set([
  // "PASTE_ADMIN_UID_HERE",
]);

const configIsPlaceholder = FIREBASE_CONFIG.apiKey === "REPLACE_ME";

let app, auth, db;
if (!configIsPlaceholder) {
  app = initializeApp(FIREBASE_CONFIG);
  auth = getAuth(app);
  db = getFirestore(app);
}

const googleProvider = new GoogleAuthProvider();

async function signInWithGoogle() {
  if (configIsPlaceholder) {
    alert(
      "Firebase isn't configured yet. Edit docs/shared.js and paste the\n" +
      "Firebase config from your Firebase console (project settings → your apps)."
    );
    return null;
  }
  const result = await signInWithPopup(auth, googleProvider);
  return result.user;
}

async function signOut() {
  if (auth) await fbSignOut(auth);
}

function onAuth(callback) {
  if (configIsPlaceholder) {
    // Drive the UI through the placeholder path so the page still renders.
    callback(null);
    return () => {};
  }
  return onAuthStateChanged(auth, callback);
}

function isAdmin(user) {
  return !!(user && ADMIN_UIDS.has(user.uid));
}

// Expose to non-module scripts on the page if needed.
window.fwc = {
  auth, db,
  signInWithGoogle, signOut, onAuth, isAdmin,
  configIsPlaceholder,
  // Re-export Firestore helpers commonly used on pages, so individual pages
  // don't have to repeat the import URL.
  doc, getDoc, setDoc,
};
