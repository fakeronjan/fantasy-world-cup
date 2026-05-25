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
  // Render the global state banner on every page (best-effort; silently
  // no-ops if the page doesn't have a #stateBanner element).
  setTimeout(async () => {
    const el = document.getElementById('stateBanner');
    if (!el) return;
    const state = await getGameState();
    renderStateBanner(el, state);
  }, 0);
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
// Read live game state from Firestore. Returns one of:
//   { state: 'config-missing' }                     — Firebase not configured
//   { state: 'pre-kickoff', kickoff: <iso|null> }   — drafts open, no tourney
//   { state: 'round-in-progress', round: 'R32' }    — match in progress, rosters locked
//   { state: 'window-open', round: 'R32' }          — transfer window open for round R32
//   { state: 'done' }                               — tournament complete
async function getGameState() {
  if (configIsPlaceholder) return { state: 'config-missing' };
  try {
    const snap = await getDoc(doc(db, 'config', 'global'));
    const c = snap.exists() ? snap.data() : {};
    const round = c.currentRound || 'pre';
    if (round === 'done') return { state: 'done' };
    if (round === 'pre')  return { state: 'pre-kickoff', kickoff: c.kickoffTimestamp || null };
    if (c.transferWindowOpen === true) return { state: 'window-open', round };
    return { state: 'round-in-progress', round };
  } catch (e) {
    console.error('getGameState failed:', e);
    return { state: 'unknown' };
  }
}

// Render the game-state banner into a target element. Visual treatment:
//   pre-kickoff       → teal (drafting allowed)
//   round-in-progress → gray (rosters locked)
//   window-open       → pink/accent (action!)
//   done              → pink/accent (final)
function renderStateBanner(targetEl, state) {
  if (!targetEl || !state) return;
  const ROUND_NAMES = {
    'group': 'Group stage', 'R32': 'Round of 32', 'R16': 'Round of 16',
    'QF': 'Quarter-finals', 'SF': 'Semi-finals', 'F': 'Final',
  };
  let bg, color, icon, title, sub;
  if (state.state === 'config-missing') return;
  if (state.state === 'pre-kickoff') {
    bg = '#e0f2fe'; color = '#075985'; icon = '⏳';
    title = 'Pre-kickoff';
    sub = state.kickoff
      ? `Drafts open until ${formatKickoff(state.kickoff)}`
      : 'Drafts open — kickoff time TBD';
  } else if (state.state === 'round-in-progress') {
    bg = '#f3f4f6'; color = '#374151'; icon = '🔒';
    title = `${ROUND_NAMES[state.round] || state.round} in progress`;
    sub = `Rosters locked until the next transfer window opens.`;
  } else if (state.state === 'window-open') {
    bg = '#fef0f7'; color = '#9d174d'; icon = '🟢';
    title = `Transfer window OPEN · ${ROUND_NAMES[state.round] || state.round}`;
    sub = `Make your moves on the Transfer page. Closes 1h before the next match.`;
  } else if (state.state === 'done') {
    bg = '#fef0f7'; color = '#9d174d'; icon = '🏆';
    title = 'Tournament complete';
    sub = 'Final standings on the Leaderboard.';
  } else {
    return; // unknown state — skip
  }
  targetEl.innerHTML = `
    <div style="background:${bg}; color:${color}; padding:10px 14px; border-radius:6px;
                margin-bottom:16px; font-size:13px; display:flex; align-items:center; gap:10px">
      <span style="font-size:18px">${icon}</span>
      <div style="flex:1">
        <strong>${title}</strong>
        <span style="opacity:0.85; margin-left:6px">· ${sub}</span>
      </div>
    </div>`;
}

function formatKickoff(iso) {
  try {
    const d = iso?.toDate ? iso.toDate() : new Date(iso);
    return d.toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
      timeZoneName: 'short',
    });
  } catch { return 'kickoff'; }
}

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
  getGameState, renderStateBanner,
  configIsPlaceholder,
  // Re-export Firestore helpers commonly used on pages, so individual pages
  // don't have to repeat the import URL.
  doc, getDoc, setDoc,
};
