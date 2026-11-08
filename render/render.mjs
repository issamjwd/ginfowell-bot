// Remotion text overlay for liminality Shorts. Called by bot/overlay.py; never uploads anything.
//
//   node render.mjs --props props.json --out overlay.mp4
//
// props.json is built by bot/overlay.py (props_from_storyboard) plus "videoPath": the CLEAN video (no burned text).
// Fonts, colours and caption layout come from ../assets/style.json (the same file the Python side reads).
// The browser is NEVER downloaded: REMOTION_BROWSER_EXECUTABLE must point at a local Chrome (on GitHub's
// ubuntu-24.04 runner: the preinstalled /usr/bin/google-chrome). One browser instance is reused for the whole render.
// Output is video only (muted); bot/overlay.py muxes the clean video's own audio back in.
import {bundle} from '@remotion/bundler';
import {openBrowser, renderFrames, renderMedia, selectComposition} from '@remotion/renderer';
import {copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FONTS_DIR = path.join(HERE, '..', 'assets', 'fonts');
const STYLE_FILE = path.join(HERE, '..', 'assets', 'style.json');

const args = {};
for (let i = 2; i < process.argv.length; i++) {
	if (process.argv[i].startsWith('--')) args[process.argv[i].slice(2)] = process.argv[i + 1];
}

// style.json -> {colours, caption, fonts: [{family, file, weight}]} with the first heading font that exists
export function resolveStyle(style, fontsDir = FONTS_DIR) {
	const head = style.fonts.head;
	const headFile = head.files.find((f) => existsSync(path.join(fontsDir, f))) ?? head.files[head.files.length - 1];
	const fonts = [{family: head.family, file: headFile, weight: head.weight}];
	for (const [weight, file] of Object.entries(style.fonts.body.files)) {
		fonts.push({family: style.fonts.body.family, file, weight});
	}
	return {colours: style.colours, caption: style.caption, fonts, head: head.family, body: style.fonts.body.family};
}

async function main() {
	if (!args.props || !args.out) throw new Error('usage: node render.mjs --props props.json --out overlay.mp4');
	const browserExecutable = process.env.REMOTION_BROWSER_EXECUTABLE;
	if (!browserExecutable || !existsSync(browserExecutable)) {
		console.error('REMOTION_BROWSER_EXECUTABLE is not set to a local Chrome; refusing to download one');
		process.exit(3);
	}
	const props = JSON.parse(readFileSync(args.props, 'utf8'));
	// a long video whose ranges are ALL text-only (props.alpha all true) never shows the picture: the overlay frames
	// are rendered first and ffmpeg lays them over the picture while it is being made, so there is no clean video yet
	const allAlpha = Array.isArray(props.ranges) && props.ranges.length > 0 && Array.isArray(props.alpha)
		&& props.alpha.length === props.ranges.length && props.alpha.every((a) => a === true);
	if (!allAlpha && (!props.videoPath || !existsSync(props.videoPath))) {
		throw new Error('props.videoPath (the clean video) is missing');
	}
	const style = resolveStyle(JSON.parse(readFileSync(STYLE_FILE, 'utf8')));

	// Remotion serves media from its public dir over its own local http server: a private temp copy per render.
	const pub = mkdtempSync(path.join(os.tmpdir(), 'liminality-overlay-'));
	const t0 = performance.now();
	let browser = null;
	try {
		mkdirSync(path.join(pub, 'fonts'));
		for (const f of style.fonts) copyFileSync(path.join(FONTS_DIR, f.file), path.join(pub, 'fonts', f.file));
		if (!allAlpha) copyFileSync(props.videoPath, path.join(pub, 'bg.mp4'));
		// the episode's accent (props.colours, bot/palette.py) over style.json's: one accent per video, a new one each time
		const inputProps = {...props, video: 'bg.mp4', style: {...style, colours: {...style.colours, ...(props.colours || {})}}};
		delete inputProps.videoPath;

		const chromeMode = 'chrome-for-testing'; // full Chrome binary: --headless=new
		browser = await openBrowser('chrome', {browserExecutable, chromeMode, logLevel: 'warn'});
		const serveUrl = await bundle({entryPoint: path.join(HERE, 'src', 'index.jsx'), publicDir: pub});
		const common = {serveUrl, inputProps, puppeteerInstance: browser, browserExecutable, chromeMode, logLevel: 'warn'};
		// props.ranges (the long video): render only those inclusive frame ranges, one output each:
		//   alpha range  (props.alpha[i], text only, no picture behind it): a folder of transparent PNG frames,
		//                <out>.part<i>/element-NNNN.png; bot/overlay.py lays them over the clean video with ffmpeg
		//   opaque range (it holds the film grain, which blends with the picture): the picture is rendered in too,
		//                <out>.part<i>.mp4
		// Otherwise (a Short): the whole composition as one opaque mp4.
		const ranged = Array.isArray(props.ranges) && props.ranges.length;
		const jobs = ranged
			? props.ranges.map((r, i) => ({
				frameRange: [r[0], r[1]],
				alpha: Array.isArray(props.alpha) && props.alpha[i] === true,
				out: args.out.replace(/\.mp4$/, '') + `.part${i}`,
			}))
			: [{frameRange: null, alpha: false, out: args.out}];
		// every runner core works on frames (Remotion's default uses half of them)
		const concurrency = Number(process.env.REMOTION_CONCURRENCY) || Math.min(8, Math.max(1, os.cpus().length));
		let last = -1;
		for (const [j, job] of jobs.entries()) {
			const jobProps = {...inputProps, transparent: job.alpha};
			const composition = await selectComposition({...common, inputProps: jobProps, id: 'LiminalityShort'});
			const progress = ({progress}) => {
				const p = Math.floor(((j + progress) / jobs.length) * 10);
				if (p !== last) {
					last = p;
					console.log(`render ${p * 10}%`);
				}
			};
			const shared = {
				...common,
				inputProps: jobProps,
				composition,
				concurrency,
				frameRange: job.frameRange,
				onBrowserDownload: () => {
					throw new Error('browser download attempted; refusing');
				},
			};
			if (job.alpha) {
				await renderFrames({
					...shared,
					imageFormat: 'png',
					outputDir: path.resolve(job.out),
					onStart: () => {},
					onFrameUpdate: (done) => progress({progress: done / (job.frameRange[1] - job.frameRange[0] + 1)}),
				});
				continue;
			}
			await renderMedia({
				...shared,
				codec: 'h264',
				crf: jobs.length > 1 ? 16 : 18, // range parts are encoded once more by the bot: keep them a step cleaner
				x264Preset: jobs.length > 1 ? 'veryfast' : 'medium',
				// a Short is uploaded as is: cap it near YouTube's 1080p advice (film grain at crf 18 ran to ~25 Mbit/s,
				// 40 MB for 13 s, and the free plan holds only 500 MB of artifacts)
				...(jobs.length > 1 ? {} : {encodingMaxRate: '12M', encodingBufferSize: '24M'}),
				pixelFormat: 'yuv420p',
				colorSpace: 'bt709', // limited (tv) range BT.709; the default comes out full-range yuvj420p
				muted: true,
				onProgress: progress,
				outputLocation: path.resolve(job.out + (ranged ? '.mp4' : '')),
			});
		}
		console.log(`overlay done in ${((performance.now() - t0) / 1000).toFixed(1)} s`);
	} finally {
		if (browser) await browser.close({silent: true}).catch(() => {});
		rmSync(pub, {recursive: true, force: true});
	}
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	main().catch((e) => {
		console.error(e?.stack || String(e));
		process.exit(1);
	});
}
