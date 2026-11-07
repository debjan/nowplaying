// Dashboard entry point, loaded as a single `<script type="module">` at the end
// of <body>. The browser resolves the import graph and executes the modules in
// dependency order, so these imports only choose *what* runs, not the order;
// each module pulls in what it needs itself. The pre-paint theme bootstrap is
// deliberately not part of the graph: it stays a classic blocking script in
// <head> (a module would be deferred and paint the wrong colours first).

import './core.js';
import './state.js';
import './playback.js';
import './progress.js';
import './feedback.js';
import './sleep.js';
import './settings.js';
import './stream.js';
import './dialogs.js';
import './themes.js';
