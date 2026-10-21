// node render.js <job.json>: render each item of the job as a clip with alpha.
//
// Run by integrations/reel_studio/motion/runtime.py (render), in the folder
// RuntimeToolManager set this project up in (its 'reel_motion' row), after
// prepare.js bundled the templates into ./bundle.  The job is
//   {"concurrency": n, "timeout_ms": t,
//    "items": [{"id": "<composition id in templates.js>",
//               "props": {..., "width", "height", "fps", "durationInFrames"},
//               "out": "<absolute .mov path>"}]}
// Each item becomes a silent ProRes 4444 clip (yuva444p10le, PNG frames:
// Remotion's settings for a transparent video), the layout's size and
// length.  One browser serves every item.  One JSON object per stdout line;
// exit 0 only when every clip was written.  HARTOS, Apache-2.0.
const fs = require('fs');
const path = require('path');
const {openBrowser, renderMedia, selectComposition} = require('@remotion/renderer');

const say = (event) => console.log(JSON.stringify(event));

async function main() {
  const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const serveUrl = path.join(__dirname, 'bundle');
  const timeoutInMilliseconds = job.timeout_ms || 60000;
  const browser = await openBrowser('chrome', {logLevel: 'error'});
  try {
    for (const item of job.items) {
      const inputProps = item.props;
      const composition = await selectComposition({
        serveUrl, id: item.id, inputProps, timeoutInMilliseconds, logLevel: 'error',
        puppeteerInstance: browser,
      });
      let lastTenth = -1;
      await renderMedia({
        composition, serveUrl, inputProps, outputLocation: item.out, overwrite: true,
        codec: 'prores', proResProfile: '4444', pixelFormat: 'yuva444p10le', imageFormat: 'png',
        muted: true, concurrency: job.concurrency || 1, timeoutInMilliseconds, logLevel: 'error',
        puppeteerInstance: browser,
        onProgress: ({progress}) => {
          const tenth = Math.floor(progress * 10);
          if (tenth !== lastTenth) {
            lastTenth = tenth;
            say({event: 'progress', id: item.id, percent: Math.round(progress * 100)});
          }
        },
      });
      say({event: 'rendered', id: item.id, out: item.out});
    }
  } finally {
    await browser.close({silent: true});
  }
}

main().catch((error) => {
  console.error(String((error && error.stack) || error));
  process.exit(1);
});
