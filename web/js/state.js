// Shared mutable state for the dashboard modules.
//
// ES module imports are read-only *bindings* (an importer cannot rebind
// `import { durationMs }`), so the state that several modules write lives in
// one exported object: importers may read and assign its properties, but the
// object itself is never replaced.

export const state = {
    // Track duration from the SSE payload (ms, 0 when unknown) and the
    // position the progress bar / OS scrubber advance between reconciliations.
    durationMs: 0,
    positionMs: 0,
    lastTickTs: Date.now(),
    // Whether the position counter is running (true while a track is shown),
    // whether the server already confirmed a position, and whether a player
    // backend is reachable (gates the progress bar; the transport controls
    // latch on once a player has been seen - see stream.js).
    progressActive: false,
    positionSynced: false,
    playerctlAvailable: false,
    // Latest track payload rendered by stream.js; feedback.js re-renders and
    // records verdicts against it, so both must see the same object.
    lastTrackData: null,
};
