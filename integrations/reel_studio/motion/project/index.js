// Reel Studio's animated overlays: the entry Remotion's bundler starts from
// (prepare.js bundles it once per setup).  HARTOS, Apache-2.0.
const {registerRoot} = require('remotion');
const {Root} = require('./templates');

registerRoot(Root);
