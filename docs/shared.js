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

import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import {
  getAuth,
  GoogleAuthProvider,
  signInWithPopup,
  signInWithRedirect,
  getRedirectResult,
  signOut as fbSignOut,
  onAuthStateChanged,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";
import {
  getFirestore,
  doc,
  getDoc,
  setDoc,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";

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
  "mixdmNZGD3YnzSFOJ077AKfQKXL2",  // rjsikdar@gmail.com
]);

const configIsPlaceholder = FIREBASE_CONFIG.apiKey === "REPLACE_ME";

let app, auth, db;
if (!configIsPlaceholder) {
  app = initializeApp(FIREBASE_CONFIG);
  auth = getAuth(app);
  db = getFirestore(app);
}

const googleProvider = new GoogleAuthProvider();
// Explicitly request both scopes — older SDKs default to "profile" only,
// which Firebase Auth's handler can reject as missing the email field it
// needs to identify the user. Setting both makes the request unambiguous.
googleProvider.addScope('profile');
googleProvider.addScope('email');

// Mobile + in-app browsers (iOS Safari, Instagram/Spotify webviews) break
// signInWithPopup because they sandbox popups in ways that prevent the
// post-auth handshake. Detect those and use the redirect flow instead —
// full-page navigation, no cross-origin cookies needed.
function shouldUseRedirect() {
  const ua = navigator.userAgent || '';
  const isMobile = /Mobi|Android|iPhone|iPad|iPod/i.test(ua);
  const isInAppBrowser = /Instagram|FBAN|FBAV|Line|MicroMessenger|Twitter|Snapchat|Spotify/i.test(ua);
  return isMobile || isInAppBrowser;
}

async function signInWithGoogle() {
  if (configIsPlaceholder) {
    alert(
      "Firebase isn't configured yet. Edit docs/shared.js and paste the\n" +
      "Firebase config from your Firebase console (project settings → your apps)."
    );
    return null;
  }
  if (shouldUseRedirect()) {
    // Full-page redirect to Google; on return, getRedirectResult() picks up
    // the result during module init below and onAuthStateChanged fires.
    await signInWithRedirect(auth, googleProvider);
    return null;
  }
  try {
    const result = await signInWithPopup(auth, googleProvider);
    return result.user;
  } catch (e) {
    // Popup blocked, closed by user, or browser cookie restrictions —
    // fall back to redirect flow rather than failing outright.
    if (e?.code === 'auth/popup-blocked' ||
        e?.code === 'auth/popup-closed-by-user' ||
        e?.code === 'auth/cancelled-popup-request') {
      await signInWithRedirect(auth, googleProvider);
      return null;
    }
    throw e;
  }
}

// Pick up any pending redirect result when the page loads (no-op on
// pages that didn't initiate auth). onAuthStateChanged still fires for
// successful sign-ins, so callers don't need to handle this manually.
if (!configIsPlaceholder) {
  getRedirectResult(auth).catch((e) => {
    // Suppress noise — auth state listener will reflect real errors.
    console.debug('getRedirectResult:', e?.code || e);
  });
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

// Single source of truth for "what do we call this user in the UI?"
// Priority: their custom league nickname → Google account displayName →
// email local-part (half-masked) → first 6 chars of UID.
// Accepts a Firestore user doc (with uid added) — works in both leaderboard
// rows and self-display contexts.
function nameFor(userDoc) {
  if (!userDoc) return 'Unknown';
  const nick = (userDoc.leagueNickname || '').trim();
  if (nick) return nick;
  const dn = (userDoc.displayName || '').trim();
  if (dn) return dn;
  if (userDoc.email) return userDoc.email.split('@')[0];
  return `Player ${(userDoc.uid || '').slice(0, 6)}`;
}

// Expose to non-module scripts on the page if needed.
window.fwc = {
  auth, db,
  signInWithGoogle, signOut, onAuth, isAdmin, nameFor,
  configIsPlaceholder,
  // Re-export Firestore helpers commonly used on pages, so individual pages
  // don't have to repeat the import URL.
  doc, getDoc, setDoc,
};
