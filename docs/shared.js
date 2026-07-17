// Fantasy World Cup - shared Firebase init + auth helpers.
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

// Firebase web config. These values are intentionally PUBLIC - security
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

// Admin uid(s) - these accounts can write to teams, players, matches, config.
// Will be enforced by Firestore security rules. Populate after first sign-in
// (sign in once with the admin Google account, look up the uid in Firebase
// console → Authentication, paste it here AND in firestore.rules).
export const ADMIN_UIDS = new Set([
  "mixdmNZGD3YnzSFOJ077AKfQKXL2",  // rjsikdar@gmail.com
]);

const configIsPlaceholder = FIREBASE_CONFIG.apiKey === "REPLACE_ME";

// Flag Knockout launch gate. The single source of truth is the boolean
// `flagContest/state.launched` (toggled from the Admin panel - no redeploy).
// While it's false, the Flags nav tab, the flags.html page, and every promo are
// visible ONLY to admins, so the contest can be previewed privately. Flip it on
// and everything (web + emails) turns on for everyone at once.
let _flagStateCache;   // undefined = not fetched; null = no doc / not signed in
async function getFlagState() {
  if (_flagStateCache !== undefined) return _flagStateCache;
  if (configIsPlaceholder) return (_flagStateCache = null);
  try {
    const snap = await getDoc(doc(db, 'flagContest', 'state'));
    _flagStateCache = snap.exists() ? snap.data() : null;
  } catch { _flagStateCache = null; }
  return _flagStateCache;
}
function flagsVisibleTo(user, st) {
  return !!(user && ADMIN_UIDS.has(user.uid)) || !!(st && st.launched);
}
async function gateFlagsTab(user) {
  const fl = document.getElementById('flagsLink');
  if (!fl) return;
  // Admins see the tab immediately; others only once the contest is launched.
  if (user && ADMIN_UIDS.has(user.uid)) { fl.style.display = ''; return; }
  const st = await getFlagState();
  fl.style.display = flagsVisibleTo(user, st) ? '' : 'none';
}

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
    gateFlagsTab(null);
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
  // Wrap the page callback to also gate the preview-only Flags tab on every page.
  return onAuthStateChanged(auth, (user) => { gateFlagsTab(user); callback(user); });
}

function isAdmin(user) {
  return !!(user && ADMIN_UIDS.has(user.uid));
}

// Single source of truth for "what do we call this user in the UI?"
// Priority: their custom league nickname → Google account displayName →
// email local-part (half-masked) → first 6 chars of UID.
// Accepts a Firestore user doc (with uid added) - works in both leaderboard
// rows and self-display contexts.
// Read live game state from Firestore. Returns one of:
//   { state: 'config-missing' }                     - Firebase not configured
//   { state: 'pre-kickoff', kickoff: <iso|null> }   - drafts open, no tourney
//   { state: 'round-in-progress', round: 'R32' }    - match in progress, rosters locked
//   { state: 'window-open', round: 'R32' }          - transfer window open for round R32
//   { state: 'done' }                               - tournament complete
async function getGameState() {
  if (configIsPlaceholder) return { state: 'config-missing' };
  try {
    const snap = await getDoc(doc(db, 'config', 'global'));
    const c = snap.exists() ? snap.data() : {};
    const round = c.currentRound || 'pre';
    const kickoff = c.kickoffTimestamp;
    // The TRUE roster-lock signal is "now >= kickoffTimestamp", not the
    // currentRound label. If currentRound got bumped early (manual admin
    // change, false auto-transition), we still want to show pre-kickoff
    // until the actual kickoff moment.
    const kickoffDate = kickoff?.toDate
      ? kickoff.toDate()
      : (kickoff ? new Date(kickoff) : null);
    const beforeKickoff = !kickoffDate || new Date() < kickoffDate;

    if (round === 'done') return { state: 'done' };
    if (beforeKickoff)    return { state: 'pre-kickoff', kickoff };
    // Mid-transition: the round just turned over. While the settle lock holds
    // (window still closed) show a "settling" banner; once trading opens, keep
    // the transition flag so the window-open banner warns trades may be reverted.
    const transition = c.transitionState === true;
    if (transition && c.transferWindowOpen !== true) return { state: 'transition-settling', round };
    if (c.transferWindowOpen === true) return { state: 'window-open', round, transition };
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
      : 'Drafts open - kickoff time TBD';
  } else if (state.state === 'round-in-progress') {
    bg = '#f3f4f6'; color = '#374151'; icon = '🔒';
    title = `${ROUND_NAMES[state.round] || state.round} in progress`;
    sub = `Rosters locked until the next transfer window opens.`;
  } else if (state.state === 'transition-settling') {
    bg = '#fef3c7'; color = '#92400e'; icon = '⚙️';
    title = `Round transition in progress · ${ROUND_NAMES[state.round] || state.round}`;
    sub = `Results and modeling are settling - transfers reopen shortly.`;
  } else if (state.state === 'window-open') {
    bg = '#fef0f7'; color = '#9d174d'; icon = '🟢';
    title = `Transfer window OPEN · ${ROUND_NAMES[state.round] || state.round}`;
    sub = state.transition
      ? `Transfers are LIVE. Heads up: the game is in a transition state, so in the rare event of a bracket/scoring error, transfers made now may be reverted.`
      : `Make your moves on the Transfer page. Closes 1h before the next match.`;
  } else if (state.state === 'done') {
    bg = '#fef0f7'; color = '#9d174d'; icon = '🏆';
    title = 'Tournament complete';
    sub = 'Final standings on the Leaderboard.';
  } else {
    return; // unknown state - skip
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

// Country flag emoji a user picked to rep. Empty string if none.
function flagFor(userDoc) {
  if (!userDoc) return '';
  return (userDoc.countryFlag || '').trim();
}

// Scoring weights (config.scoringWeights), read once and cached.
let _weightsCache = null;
async function getScoringWeights() {
  if (_weightsCache) return _weightsCache;
  try {
    const snap = await getDoc(doc(db, 'config', 'global'));
    _weightsCache = (snap.exists() && snap.data().scoringWeights) || {};
  } catch { _weightsCache = {}; }
  return _weightsCache;
}

// Knockout round-start schedule (config.roundStartsUtc, published every run by
// the ingest cron via _fwc_lib.ROUND_FIRST_KICKOFF_UTC). Read once and cached.
// Falls back to the canonical WC2026 schedule if the field hasn't been written
// yet, so schedule-driven UI never has to guess a date.
const ROUND_STARTS_FALLBACK = {
  R32: '2026-06-28T19:00:00Z', R16: '2026-07-04T17:00:00Z',
  QF:  '2026-07-09T20:00:00Z', SF:  '2026-07-14T19:00:00Z',
  F:   '2026-07-19T19:00:00Z',
};
let _scheduleCache = null;
async function getRoundSchedule() {
  if (_scheduleCache) return _scheduleCache;
  try {
    const snap = await getDoc(doc(db, 'config', 'global'));
    const rs = (snap.exists() && snap.data().roundStartsUtc) || null;
    _scheduleCache = (rs && Object.keys(rs).length) ? rs : ROUND_STARTS_FALLBACK;
  } catch { _scheduleCache = ROUND_STARTS_FALLBACK; }
  return _scheduleCache;
}

// Itemized points breakdown for a team/player asset doc, using the SAME fields
// the ingest scores from (winsPlayedIn, cleanSheetsPlayedIn, etc. - NOT the
// vestigial cleanSheets field). Returns [{label, detail, pts}]. Reconciles to
// the asset's totalPoints: any unexplained remainder is added as an "Other"
// line so EVERY point is always accounted for on the card.
function pointsBreakdown(asset, kind, w) {
  w = w || {};
  const lines = [];
  const total = Math.round(asset.totalPoints || 0);
  if (kind === 'team') {
    const wins = asset.matchesWon || 0, draws = asset.matchesDrawn || 0;
    const winW = w.team_win ?? 3, drawW = w.team_draw ?? 1;
    if (wins)  lines.push({ label: 'Wins',  detail: `${wins} × ${winW}`,  pts: wins * winW });
    if (draws) lines.push({ label: 'Draws', detail: `${draws} × ${drawW}`, pts: draws * drawW });
    const order = ['group', 'R32', 'R16', 'QF', 'SF', 'F', 'W'];
    const bonusKey = { R32: 'bonus_r32', R16: 'bonus_r16', QF: 'bonus_qf', SF: 'bonus_sf', F: 'bonus_final', W: 'bonus_champion' };
    const bonusLabel = { R32: 'Reached R32', R16: 'Reached R16', QF: 'Reached QF', SF: 'Reached SF', F: 'Reached Final', W: 'Champion' };
    const idx = order.indexOf(asset.finalRound || 'group');
    for (let i = 1; i <= idx; i++) {
      const r = order[i];
      const pts = w[bonusKey[r]] ?? 0;
      if (pts) lines.push({ label: bonusLabel[r], detail: '', pts });
    }
  } else {
    const goals = asset.goals || 0, assists = asset.assists || 0;
    const cs = asset.cleanSheetsPlayedIn || 0, ws = asset.winsPlayedIn || 0;
    const pos = (asset.position || '').toUpperCase();
    const goalW = w.player_goal ?? 5, astW = w.player_assist ?? 3, wsW = w.player_win_share ?? 1;
    const csW = pos === 'GK' ? (w.player_clean_sheet_gk ?? 5)
              : pos === 'DEF' ? (w.player_clean_sheet_def ?? 2)
              : (w.player_clean_sheet_other ?? 0);
    if (goals)        lines.push({ label: 'Goals',   detail: `${goals} × ${goalW}`,   pts: goals * goalW });
    if (assists)      lines.push({ label: 'Assists', detail: `${assists} × ${astW}`,  pts: assists * astW });
    if (cs && csW)    lines.push({ label: `Clean sheet${pos ? ' (' + pos + ')' : ''}`, detail: `${cs} × ${csW}`, pts: cs * csW });
    if (ws)           lines.push({ label: 'Win share', detail: `${ws} × ${wsW}`, pts: ws * wsW });
  }
  const sum = lines.reduce((s, l) => s + l.pts, 0);
  if (sum !== total) lines.push({ label: 'Other', detail: '', pts: total - sum });
  return lines;
}

// All FINISHED matches, cached in localStorage by the sync timestamp (they
// only change when the ingest cron runs), so repeat use is read-light.
async function getFinishedMatches() {
  let syncedAt = null;
  try {
    const s = await getDoc(doc(db, 'leaderboard', 'snapshot'));
    syncedAt = s.exists() ? (s.data().updatedAt || null) : null;
  } catch { /* ignore */ }
  const CACHE = 'fwc_finished_matches';
  if (syncedAt) {
    try {
      const c = JSON.parse(localStorage.getItem(CACHE) || 'null');
      if (c && c.ts === syncedAt && Array.isArray(c.m)) return c.m;
    } catch { /* fall through */ }
  }
  try {
    const { collection, getDocs, query, where } = await import(
      "https://www.gstatic.com/firebasejs/10.13.0/firebase-firestore.js"
    );
    const snap = await getDocs(query(collection(db, 'matches'), where('status', '==', 'FINISHED')));
    const m = snap.docs.map(d => d.data());
    if (syncedAt) { try { localStorage.setItem(CACHE, JSON.stringify({ ts: syncedAt, m })); } catch {} }
    return m;
  } catch { return []; }
}

// Game-by-game log for an asset, replicating the ingest's per-match scoring so
// the per-match points tie out to the asset's breakdown/total. Returns rows
// sorted oldest->newest: {kickoff, oppId, oppName, gf, ga, result, note, pts}.
function assetMatchLog(asset, kind, matches, w) {
  w = w || {};
  const rows = [];
  if (kind === 'team') {
    const tid = asset.id;
    for (const m of (matches || [])) {
      const isHome = m.team1Id === tid, isAway = m.team2Id === tid;
      if (!isHome && !isAway) continue;
      const gf = isHome ? m.score1 : m.score2, ga = isHome ? m.score2 : m.score1;
      const won = (m.winner === 'HOME_TEAM' && isHome) || (m.winner === 'AWAY_TEAM' && isAway);
      const lost = (m.winner === 'HOME_TEAM' && isAway) || (m.winner === 'AWAY_TEAM' && isHome);
      const result = won ? 'W' : lost ? 'L' : 'D';
      const pts = won ? (w.team_win ?? 3) : (result === 'D' ? (w.team_draw ?? 1) : 0);
      rows.push({
        kickoff: m.kickoff || m.utcDate, oppId: isHome ? m.team2Id : m.team1Id,
        oppName: isHome ? (m.team2Name || m.team2Id) : (m.team1Name || m.team1Id),
        gf, ga, result, note: '', pts,
      });
    }
  } else {
    const fd = asset.fdId, tid = asset.teamId, pos = (asset.position || '').toUpperCase();
    const csW = pos === 'GK' ? (w.player_clean_sheet_gk ?? 5)
              : pos === 'DEF' ? (w.player_clean_sheet_def ?? 2) : (w.player_clean_sheet_other ?? 0);
    for (const m of (matches || [])) {
      const isHome = m.team1Id === tid, isAway = m.team2Id === tid;
      if (!isHome && !isAway) continue;
      const lineup = isHome ? (m.homeLineupFdIds || []) : (m.awayLineupFdIds || []);
      const played = lineup.includes(fd);
      const goals = (m.goals || []).filter(g => g.scorerFdId === fd).length;
      const assists = (m.goals || []).filter(g => g.assistFdId === fd).length;
      if (!played && !goals && !assists) continue; // not involved in this match
      const teamWon = (m.winner === 'HOME_TEAM' && isHome) || (m.winner === 'AWAY_TEAM' && isAway);
      const cs = isHome ? m.cleanSheetHome : m.cleanSheetAway;
      const winShare = played && teamWon ? 1 : 0;
      const cleanSheet = played && cs ? 1 : 0;
      const pts = goals * (w.player_goal ?? 5) + assists * (w.player_assist ?? 3)
                + winShare * (w.player_win_share ?? 1) + cleanSheet * csW;
      const bits = [];
      if (goals) bits.push(goals + 'G');
      if (assists) bits.push(assists + 'A');
      if (cleanSheet) bits.push('CS');
      if (winShare) bits.push('win');
      if (!bits.length) bits.push(played ? 'played' : 'sub');
      rows.push({
        kickoff: m.kickoff || m.utcDate, oppId: isHome ? m.team2Id : m.team1Id,
        oppName: isHome ? (m.team2Name || m.team2Id) : (m.team1Name || m.team1Id),
        gf: isHome ? m.score1 : m.score2, ga: isHome ? m.score2 : m.score1,
        result: '', note: bits.join(' '), pts,
      });
    }
  }
  rows.sort((a, b) => String(a.kickoff || '').localeCompare(String(b.kickoff || '')));
  return rows;
}

// Colorful cross-promo banner for the Flag Knockout, dropped onto other pages
// (roster/leaderboard/transfer). Renders into targetEl only when the contest is
// visible to this viewer (launched, or admin previewing). CTA is state-aware.
const _FLAG_RL = {
  wildcard: 'Wildcard', R32: 'Round of 32', R16: 'Round of 16',
  QF: 'Quarter-finals', SF: 'Semi-finals', F: 'Final',
};
async function renderFlagPromo(targetEl, user) {
  if (!targetEl) return;
  const st = await getFlagState();
  if (!st || !flagsVisibleTo(user, st)) { targetEl.innerHTML = ''; return; }
  let msg, sub, cta;
  if (st.status === 'done' && st.champion) {
    msg = `🏆 ${st.champion.name} has the best flag in the world`;
    sub = 'See how the Flag Knockout bracket played out';
    cta = 'See results →';
  } else if (st.votingOpen) {
    msg = "Vote: what's the best flag in the world?";
    sub = `Flag Knockout · ${_FLAG_RL[st.currentRound] || 'voting'} is LIVE – out of the pool? get your votes in`;
    cta = 'Vote →';
  } else {
    msg = "Vote: what's the best flag in the world?";
    sub = 'Flag Knockout · results are in – see which flags advanced';
    cta = 'See results →';
  }
  const preview = (!st.launched && isAdmin(user))
    ? '<span class="fp-preview">preview</span>' : '';
  const icon = (st.status === 'done' && st.champion)
    ? `<img class="fp-flag-img" src="./flags/${st.champion.iso}.svg" alt="${st.champion.name} flag">`
    : '<span class="fp-emoji">🏳️</span>';
  targetEl.innerHTML = `<a class="flag-promo" href="./flags.html">
    ${icon}
    <span class="fp-text"><span class="fp-msg">${msg}${preview}</span>
      <span class="fp-sub">${sub}</span></span>
    <span class="fp-cta">${cta}</span></a>`;
}

// Expose to non-module scripts on the page if needed.
window.fwc = {
  auth, db,
  signInWithGoogle, signOut, onAuth, isAdmin, nameFor, flagFor,
  getGameState, renderStateBanner, getScoringWeights, getRoundSchedule, pointsBreakdown,
  getFinishedMatches, assetMatchLog,
  configIsPlaceholder, getFlagState, renderFlagPromo,
  // Re-export Firestore helpers commonly used on pages, so individual pages
  // don't have to repeat the import URL.
  doc, getDoc, setDoc,
};
