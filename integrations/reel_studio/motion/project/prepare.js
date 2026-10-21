// Run once per setup by RuntimeToolManager after `npm install` (its
// 'reel_motion' row's npm_prepare, through model_storage.install_npm_project),
// in the folder the project was set up in: make sure Remotion's headless
// browser is downloaded (ensureBrowser), then bundle the templates into
// ./bundle so a render (render.js) starts without bundling.
// One JSON object per stdout line; exit 0 when both are done.  HARTOS, Apache-2.0.
const path = require('path');
const {bundle} = require('@remotion/bundler');
const {ensureBrowser} = require('@remotion/renderer');

const say = (event) => console.log(JSON.stringify(event));

async function main() {
  let lastTenth = -1;
  await ensureBrowser({
    onBrowserDownload: () => {
      say({event: 'browser_download'});
      return {
        version: null,
        onProgress: ({percent}) => {
          const tenth = Math.floor(percent * 10);
          if (tenth !== lastTenth) {
            lastTenth = tenth;
            say({event: 'browser_progress', percent: Math.round(percent * 100)});
          }
        },
      };
    },
  });
  say({event: 'browser_ready'});
  const out = await bundle({
    entryPoint: path.join(__dirname, 'index.js'),
    outDir: path.join(__dirname, 'bundle'),
    enableCaching: false,
  });
  say({event: 'ready', bundle: out});
}

main().catch((error) => {
  console.error(String((error && error.stack) || error));
  process.exit(1);
});
