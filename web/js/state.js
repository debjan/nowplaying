// Shared mutable state for the dashboard scripts. Classic scripts all
// share one global scope, so these bindings are readable/writable from
// every other module (stream, progress, feedback, dialogs).

const playBtn = document.querySelector('.btn-play');

let durationMs = 0;
let positionMs = 0;
let lastTickTs = Date.now();
let lastTrackId = null;
let progressActive = false;
let playerctlAvailable = false;
let positionSynced = false;
let lastRenderKey = null;
// Current track's MusicBrainz recording id (null when unresolved);
// `similarSeq` invalidates in-flight ListenBrainz dialog fetches.
let currentMbid = null;
let similarSeq = 0;
// Whether ListenBrainz has similar recordings for the current track (the
// title row only becomes clickable once it does) and the last payload it
// was rendered from, so the probe can re-render the rows when it answers.
let similarActive = false;
let similarProbedId = null;
let similarCheckSeq = 0;
let lastTrackData = null;
const progressBar = document.getElementById('progress');
const progressFill = document.getElementById('progress-fill');
const progressTip = document.getElementById('progress-tip');
