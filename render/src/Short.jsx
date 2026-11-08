import React, {createContext, useContext, useEffect, useId, useState} from 'react';
import {
	AbsoluteFill,
	Easing,
	Freeze,
	OffthreadVideo,
	Sequence,
	continueRender,
	delayRender,
	interpolate,
	random,
	spring,
	staticFile,
	useCurrentFrame,
	useVideoConfig,
} from 'remotion';
import {Particles} from './particles.jsx';

// Dreamcore text over a CLEAN video (the bot renders no text of its own when this overlay runs).
// Fonts, colours and caption layout: assets/style.json (props.style). Two fonts, one accent.
// House rules (PIXELHELM UI Style): no opacity fades, no gradients, no glass. Things appear by MOTION (masks, rises,
// tracking, de-blur) or hard steps. Every move is eased (bezier or spring), never linear.
// Everything is laid out on a stage (1080x1920 for Shorts, 1920x1080 for the long video: props.layout) and scaled to
// the composition size (low-res dry runs). LAYOUTS holds every position that differs between the two.
const FALLBACK_STYLE = {
	colours: {ink: '#17131D', paper: '#F6F1EA', sub: '#C8C4BE', accent: '#F2A7C3'},
	caption: {y: 0.25, max_width: 0.78, size: 60, line_height: 80, nudge_left: 30, left: 96},
	fonts: [],
	head: 'serif',
	body: 'sans-serif',
};
// Positions per layout, in stage px. Portrait = the Shorts' original numbers. Landscape keeps the same left-margin
// grid and type sizes, with the text column left of the long video's 960 px footage panel.
export const LAYOUTS = {
	// portrait: hook in the upper third, captions big on the lower-middle (style.json caption.y), the end tag where the
	// hook was; all clear of the right-hand Shorts buttons (x > 940) and the bottom title band (y > 1500)
	portrait: {w: 1080, h: 1920, hookTop: 230, hookW: 880, hookSize: [150, 128], hookHeavy: true, capW: null, capTop: null, capSize: null, capLine: null, outroTop: 560, outroW: 800, endTop: 640, endW: 860, tagTop: 300},
	// landscape (the long video, full-frame footage): hook and chapter titles on the upper-left grid, captions big on
	// the lower third (~9% of the height), the end tag upper-left over the last shots
	landscape: {w: 1920, h: 1080, hookTop: 170, hookW: 1300, hookSize: [112, 96], hookHeavy: true, capW: 1400, capTop: 800, capSize: 92, capLine: 108, outroTop: 360, outroW: 1300, endTop: 300, endW: 1200, chapterTop: 150, tagTop: 170},
};
const LayoutCtx = createContext(LAYOUTS.portrait);
const useLayout = () => useContext(LayoutCtx);

// Easing kit. OUT: entries (fast start, long settle). IN: exits. SWAY: slow drifts. SETTLE: damped spring, no bounce.
const OUT = Easing.bezier(0.16, 1, 0.3, 1);
const IN = Easing.bezier(0.55, 0, 0.75, 0.15);
const SWAY = Easing.bezier(0.45, 0, 0.55, 1);
const SETTLE = Easing.spring({damping: 26, mass: 0.6, stiffness: 180});
const CLAMP = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'};
const ramp = (f, a, b, from, to, easing = OUT) => interpolate(f, [a, b], [from, to], {...CLAMP, easing});
const scaleRamp = (f, a, b, from, to, easing = OUT) =>
	interpolate(f, [a, b], [from, to], {...CLAMP, easing, output: 'perceptual-scale'});

const rgba = (hex, a) => {
	const h = hex.replace('#', '');
	return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${a})`;
};

// Legibility on pale dream footage: a stroke painted UNDER the fill plus a tight shadow. No plate, no panel.
const inked = (c, w = 7) => ({
	WebkitTextStroke: `${w}px ${rgba(c.ink, 0.62)}`,
	paintOrder: 'stroke fill',
	textShadow: `0 3px 4px ${rgba(c.ink, 0.75)}, 0 0 22px ${rgba(c.ink, 0.45)}`,
});

// Glitch echoes re-render the same words in one flat colour (no stroke, no marker).
const Tint = createContext(null);

const useFonts = (fonts) => {
	const [handle] = useState(() => delayRender('fonts'));
	useEffect(() => {
		Promise.all(
			fonts.map(({family, file, weight}) =>
				new FontFace(family, `url(${staticFile('fonts/' + file)})`, {weight}).load().then((f) => document.fonts.add(f)),
			),
		).then(() => continueRender(handle));
		// a font that fails to load leaves delayRender pending, so the render times out and the bot falls back
	}, [fonts, handle]);
};

// Motion blur from velocity (px per frame): a directional SVG gaussian, so a sideways whip smears sideways and a
// slam smears all round. About a 180 degree shutter. Nothing is filtered while the element is still.
const MotionBlur = ({vx = 0, vy = 0, iso = 0, style, children}) => {
	const id = 'mb' + useId().replace(/[^a-zA-Z0-9]/g, '');
	const sx = Math.min(30, Math.abs(vx) * 0.32 + iso);
	const sy = Math.min(30, Math.abs(vy) * 0.32 + iso);
	const on = sx > 0.2 || sy > 0.2;
	return (
		<>
			{on ? (
				<svg width="0" height="0" style={{position: 'absolute'}} aria-hidden>
					<filter id={id} x="-40%" y="-80%" width="180%" height="260%" colorInterpolationFilters="sRGB">
						<feGaussianBlur stdDeviation={`${sx.toFixed(2)} ${sy.toFixed(2)}`} />
					</filter>
				</svg>
			) : null}
			<div style={{...style, filter: on ? `url(#${id})` : undefined}}>{children}</div>
		</>
	);
};

// A glitch hit: the block is cut into horizontal bands that jump sideways, over an ink and an accent echo.
// `amount` 0 renders the content once, untouched.
const BANDS = [
	[0, 38, 1],
	[38, 64, -0.7],
	[64, 100, 0.45],
];
const Glitch = ({amount, c, children}) => {
	if (amount <= 0) return children;
	return (
		<div style={{position: 'relative'}}>
			<div style={{position: 'absolute', inset: 0, translate: `${-amount * 0.7}px ${amount * 0.25}px`}} aria-hidden>
				<Tint.Provider value={c.ink}>{children}</Tint.Provider>
			</div>
			<div style={{position: 'absolute', inset: 0, translate: `${amount}px 0px`}} aria-hidden>
				<Tint.Provider value={c.accent}>{children}</Tint.Provider>
			</div>
			{BANDS.map(([a, b, push], i) => (
				<div
					key={i}
					style={{
						position: i === 0 ? 'relative' : 'absolute',
						inset: i === 0 ? undefined : 0,
						clipPath: `inset(${a}% -20% ${100 - b}% -20%)`,
						translate: `${push * amount * 1.2}px 0px`,
					}}
				>
					{children}
				</div>
			))}
		</div>
	);
};

// Film texture over everything: grain on twos (a fresh seed every 2 frames, like the drawn animation), a faint
// exposure flicker in hard steps and fine scanlines. All far below text contrast.
const FilmTexture = ({until, c, light = false}) => {
	const frame = useCurrentFrame();
	const {width, height} = useVideoConfig();
	const step = Math.floor(frame / 2);
	const r = random(`flicker-${step}`);
	const dip = frame >= until ? 0 : r < 0.72 ? 0 : r < 0.93 ? 0.035 : 0.07;
	return (
		<AbsoluteFill style={{pointerEvents: 'none'}}>
			<AbsoluteFill style={{background: c.ink, opacity: dip}} />
			{/* edit v4: a graded clean video already carries ffmpeg's film grain (bot/grade.py); a second, per-frame
			    feTurbulence grain on top costs Chrome time on every frame and only muddies it */}
			{light ? null : (
			<svg
				width={Math.round(width / 2)}
				height={Math.round(height / 2)}
				style={{position: 'absolute', left: 0, top: 0, scale: 2, transformOrigin: '0 0', mixBlendMode: 'overlay', opacity: 0.42}}
			>
				<filter id="grain" x="0" y="0" width="100%" height="100%">
					<feTurbulence type="fractalNoise" baseFrequency="0.82" numOctaves="2" seed={step % 997} stitchTiles="stitch" />
					<feColorMatrix type="matrix" values="0.33 0.33 0.33 0 0 0.33 0.33 0.33 0 0 0.33 0.33 0.33 0 0 0 0 0 0 1" />
				</filter>
				<rect width="100%" height="100%" filter="url(#grain)" />
			</svg>
			)}
			<svg width="100%" height="100%" style={{position: 'absolute', opacity: 0.04}}>
				<defs>
					<pattern id="scan" width="4" height="4" patternUnits="userSpaceOnUse">
						<rect width="4" height="1" fill="#000" />
					</pattern>
				</defs>
				<rect width="100%" height="100%" fill="url(#scan)" />
			</svg>
		</AbsoluteFill>
	);
};

// ------------------------------------------------------------------------------------------------ the system
// One grid for every frame of every video: text hangs off the same left margin (style.json caption.left), on the
// upper third, with the right-hand Shorts buttons and the bottom ~20% left empty. Text sits off-centre on purpose
// (tension), with lots of open frame. Every text block is underlined by the same 4 px accent rule, drawn in from the
// left edge of the frame: a leading line that carries the eye to the text. Tempo comes from the score: word gaps
// are fractions of the episode's beat (props.beat), so reveals tick along with the music.
const gridLeft = (st) => st.caption.left ?? 96;
const beatFrames = (beat, fps) => Math.max(6, (beat || 0.65) * fps);

// The leading line: grows from the left edge of the frame to `reach` px past the grid margin.
const EdgeRule = ({st, progress, reach = 120, top = 0}) => (
	<div
		style={{
			marginTop: top,
			marginLeft: -gridLeft(st),
			width: (gridLeft(st) + reach) * progress,
			height: 4,
			borderRadius: 2,
			background: st.colours.accent,
			// value contrast: a hard dark edge under the pink so the line reads on pale pink footage too
			boxShadow: `0 3px 0 ${rgba(st.colours.ink, 0.55)}`,
		}}
	/>
);

// ------------------------------------------------------------------------------------------------ hook
// Kinetic title: words land one after another on the beat's quarters (rise + tracking-in + de-blur, motion-blurred
// on the way). Then the leading line draws in from the edge and the dream's name follows its tip. Exit: the words
// lift out of their masks.
const RiseWord = ({word, t0, exitAt, frame}) => {
	const y = (f) => ramp(f - t0, 0, 13, 40, 0) + ramp(f, exitAt, exitAt + 5, 0, -30, IN);
	const local = frame - t0;
	if (local < 0) return <span style={{display: 'inline-block', visibility: 'hidden'}}>{word}</span>;
	const cut = ramp(frame, exitAt, exitAt + 5, 0, 100, IN);
	return (
		<MotionBlur
			vy={y(frame) - y(frame - 1)}
			iso={ramp(local, 0, 9, 9, 0)}
			style={{
				display: 'inline-block',
				translate: `0px ${y(frame)}px`,
				scale: scaleRamp(local, 0, 14, 1.14, 1),
				transformOrigin: '0% 70%',
				letterSpacing: `${ramp(local, 0, 18, 0.3, 0.01)}em`,
				clipPath: `inset(-30% -30% ${cut}% -30%)`,
			}}
		>
			{word}
		</MotionBlur>
	);
};

// Kinetic hooks (edit v4, props.hook.kind; one per edit style, so the openings never look alike):
//   slam    the whole line hits at once: scale 2.3 -> 1 on a stiff spring with a small overshoot, zoom-blurred while it
//           travels, an accent echo behind it for 3 frames (chase_cut, rules_list)
//   stretch the letters arrive spread wide and tall and pull together (tracking 0.7em -> 0, scaleY 1.7 -> 1), de-blurring
//           as they settle: slow and uncanny (slow_dread, monster_reveal)
//   type    a camcorder typewriter: one character a frame, a hard block cursor that blinks (found_footage)
//   rise    the per-word rise on the beat (pov_walk)
const SLAM = {damping: 11, stiffness: 260, mass: 0.7};
const SlamLine = ({text, frame, fps, exitAt, c}) => {
	const scaleAt = (f) => interpolate(spring({frame: f - 1, fps, config: SLAM}), [0, 1], [2.3, 1]);
	const scale = scaleAt(frame);
	const out = ramp(frame, exitAt, exitAt + 5, 0, 100, IN);
	const echo = frame >= 1 && frame < 4;
	return (
		<div style={{position: 'relative', visibility: frame < 1 ? 'hidden' : 'visible', clipPath: `inset(-40% -40% ${out}% -40%)`}}>
			{echo ? (
				<div aria-hidden style={{position: 'absolute', inset: 0, color: c.accent, WebkitTextStroke: '0px', textShadow: 'none', scale: scale * 1.08, transformOrigin: '0% 60%', translate: '10px 6px'}}>
					{text}
				</div>
			) : null}
			<MotionBlur iso={Math.min(18, Math.abs(scale - scaleAt(frame - 1)) * 30)} style={{position: 'relative', scale, transformOrigin: '0% 60%'}}>
				{text}
			</MotionBlur>
		</div>
	);
};

const STRETCH = {damping: 20, stiffness: 90, mass: 0.9};
const StretchLine = ({text, frame, fps, exitAt}) => {
	const s = spring({frame: frame - 1, fps, config: STRETCH});
	const out = ramp(frame, exitAt, exitAt + 5, 0, 100, IN);
	return (
		<MotionBlur
			iso={interpolate(s, [0, 1], [10, 0], CLAMP)}
			style={{
				visibility: frame < 1 ? 'hidden' : 'visible',
				letterSpacing: `${interpolate(s, [0, 1], [0.7, -0.01])}em`,
				scale: `1 ${interpolate(s, [0, 1], [1.7, 1])}`,
				transformOrigin: '0% 70%',
				clipPath: `inset(-40% -40% ${out}% -40%)`,
			}}
		>
			{text}
		</MotionBlur>
	);
};

const TypeLine = ({text, frame, exitAt, c}) => {
	const n = Math.max(0, Math.min(text.length, (frame - 1) * 2)); // two characters a frame: up inside the first second
	const cursorOn = n < text.length || Math.floor(frame / 6) % 2 === 0;
	const out = ramp(frame, exitAt, exitAt + 5, 0, 100, IN);
	return (
		<div style={{clipPath: `inset(-40% -40% ${out}% -40%)`}}>
			{text.slice(0, n)}
			<span style={{display: 'inline-block', width: '0.5em', height: '0.9em', marginLeft: 6, verticalAlign: '-0.08em', background: cursorOn ? c.accent : 'transparent'}} />
		</div>
	);
};

const Hook = ({title, label, seconds, beat, wordFrames, st, kind = 'rise'}) => {
	const L = useLayout();
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const c = st.colours;
	const total = Math.max(8, Math.round(seconds * fps));
	const words = (title || '').split(/\s+/).filter(Boolean);
	const exitAt = total - 6;
	const room = exitAt - 4; // everything is up and readable by here, even on a 1 s hook
	const gap = Math.max(1, Math.min(4, Math.round(beatFrames(beat, fps) / 4), Math.floor((room * 0.45) / Math.max(1, words.length))));
	const size = Array.from(title || '').length > 22 ? L.hookSize[1] : L.hookSize[0];
	// the line follows the last word (sooner on a short hook, so the name is fully up before the exit)
	const ruleAt = Math.max(2, Math.min(2 + words.length * gap, room - 12));
	const rule = ramp(frame, ruleAt, ruleAt + 8, 0, 1) * ramp(frame, exitAt, exitAt + 4, 1, 0, IN);
	return (
		<div
			style={{
				position: 'absolute',
				left: gridLeft(st),
				width: L.hookW,
				top: L.hookTop,
				translate: `0px ${ramp(frame, 0, total, -6, 6, SWAY)}px`,
			}}
		>
			{words.length && kind !== 'rise' ? (
				<div style={{fontFamily: st.body, fontWeight: 700, fontSize: size, lineHeight: 1.04, letterSpacing: -1, color: c.paper, ...inked(c, 10)}}>
					{kind === 'slam' ? <SlamLine text={title} frame={frame} fps={fps} exitAt={exitAt} c={c} /> : null}
					{kind === 'stretch' ? <StretchLine text={title} frame={frame} fps={fps} exitAt={exitAt} /> : null}
					{kind === 'type' ? <TypeLine text={title} frame={frame} exitAt={exitAt} c={c} /> : null}
				</div>
			) : null}
			{words.length && kind === 'rise' ? (
				<div
					style={
						L.hookHeavy
							? {fontFamily: st.body, fontWeight: 700, fontSize: size, lineHeight: 1.04, letterSpacing: -1, color: c.paper, ...inked(c, 10)}
							: {fontFamily: st.head, fontSize: size, lineHeight: 1.1, color: c.paper, ...inked(c, 8)}
					}
				>
					{words.map((w, i) => (
						<React.Fragment key={i}>
							<RiseWord word={w} t0={wordFrames?.[i] ?? 1 + i * gap} exitAt={exitAt + Math.min(i, 3) * 0.5} frame={frame} />
							{i < words.length - 1 ? ' ' : null}
						</React.Fragment>
					))}
				</div>
			) : null}
			<EdgeRule st={st} progress={rule} top={28} />
			{label ? (
				<div
					style={{
						marginTop: 20,
						fontFamily: st.body,
						fontWeight: 700,
						fontSize: 36,
						letterSpacing: 8,
						color: c.paper,
						...inked(c, 8),
						// the name is uncovered by the line's tip, then lifts out with the title
						clipPath: `inset(-20% ${ramp(frame, ruleAt + 2, ruleAt + 10, 100, 0)}% ${ramp(frame, exitAt, exitAt + 4, 0, 100, IN)}% -5%)`,
					}}
				>
					{label}
				</div>
			) : null}
		</div>
	);
};

// ------------------------------------------------------------------------------------------------ captions
// Word-level captions on the grid. Normal: each word rises out of its own mask, one every quarter beat. Whip: the
// whole line rides in locked to the picture's whip pan. Impact: the line SLAMS on the impact frame, which is also
// the score's hit (scale + blur + the band glitch, ink on the inverted frame), then rides the picture's own shake.
// One word per line gets the marker: a solid ink plate with the accent bar under it, landing on the next half beat,
// so the key word always has full value contrast whatever the footage. Exit: the words lift out of their masks.
const smooth = (x) => {
	const t = Math.min(1, Math.max(0, x));
	return t * t * (3 - 2 * t);
};
// bot/render.py's whip, from its shot-local time tl: for tl < 4 frames the new shot's left edge sits at
// W * (1 - smooth((int(tl * fps) + 1) / 5))
const whipX = (tl, fps, W) => (tl < 0 ? W : tl >= 4 / fps ? 0 : W * (1 - smooth((Math.floor(tl * fps + 1e-9) + 1) / 5)));

const CaptionWord = ({word, frame, t0, markAt, exitAt, emph, inverted, c, rise}) => {
	const tint = useContext(Tint);
	const local = frame - t0;
	const y = (f) => (rise ? ramp(f - t0, 0, 8, 30, 0) : 0) + ramp(f, exitAt, exitAt + 4, 0, -18, IN);
	const mask = rise ? ramp(local, 0, 6, 100, 0) : 0;
	const out = ramp(frame, exitAt, exitAt + 4, 0, 100, IN);
	// the marker wipes in left to right on its beat, and out right to left on the exit
	const mIn = ramp(frame, markAt, markAt + 6, 0, 1);
	const mOut = ramp(frame, exitAt - 1, exitAt + 3, 0, 1, IN);
	const marker = emph && !tint ? Math.max(0, mIn - mOut) : 0;
	const punch = emph ? interpolate(frame, [markAt, markAt + 3, markAt + 10], [1, 1.06, 1], {...CLAMP, easing: [OUT, SETTLE]}) : 1;
	const look = tint ? {color: tint} : inverted ? {color: c.ink} : {color: c.paper, ...inked(c)};
	// edit v4: each word pops in on a spring (0.6 -> a small overshoot -> 1) as it rises, so text lands with weight
	const {fps} = useVideoConfig();
	const pop = rise && local >= 0 ? interpolate(spring({frame: local, fps, config: {damping: 12, stiffness: 240, mass: 0.6}}), [0, 1], [0.6, 1]) : 1;
	const inside = mOut > 0 ? `inset(-40% -40% -40% ${(1 - marker) * 100}%)` : `inset(-40% ${(1 - marker) * 100}% -40% -40%)`;
	const outside = mOut > 0 ? `inset(-40% ${marker * 100}% -40% -40%)` : `inset(-40% -40% -40% ${marker * 100}%)`;
	return (
		<span
			style={{
				display: 'inline-block',
				position: 'relative',
				visibility: local >= 0 ? 'visible' : 'hidden',
				translate: `0px ${y(frame)}px`,
				scale: punch * pop,
				transformOrigin: '50% 85%',
				clipPath: `inset(${mask}% -40% ${out}% -40%)`,
			}}
		>
			{marker > 0 ? (
				<span
					style={{
						position: 'absolute',
						left: -12,
						right: -12,
						top: 8,
						bottom: 0,
						background: rgba(c.ink, 0.92),
						borderBottom: `4px solid ${c.accent}`,
						borderRadius: 4,
						transformOrigin: mOut > 0 ? '100% 50%' : '0% 50%',
						scale: `${marker} 1`,
					}}
				/>
			) : null}
			{/* outside the plate the word keeps its stroked look; inside it is clean paper on ink */}
			<span style={{position: 'relative', ...look, clipPath: marker > 0 ? outside : undefined}}>{word}</span>
			{marker > 0 ? (
				<span aria-hidden style={{position: 'absolute', left: 0, top: 0, color: c.paper, clipPath: inside}}>
					{word}
				</span>
			) : null}
		</span>
	);
};

const Caption = ({cap, seconds, beat, st}) => {
	const L = useLayout();
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const {colours: c, caption: lay} = st;
	const total = Math.max(1, Math.round(seconds * fps));
	const hits = cap.hits || [];
	const impact = hits.includes('impact');
	const punch = hits.includes('zoom_punch');
	const whip = cap.enter === 'whip';
	// edit styles (bot/edits.py): rise = words rise from masks; slam = the whole line at once, slammed on the hit;
	// type = words snap on in hard steps (found footage); rule = "rule 1:" label over the rule
	const kind = cap.kind || 'rise';
	let words0 = cap.text.split(/\s+/).filter(Boolean);
	let label = null;
	if (kind === 'rule' && words0.length > 2 && /^\d+:$/.test(words0[1])) {
		label = `${words0[0]} ${words0[1].slice(0, -1)}`;
		words0 = words0.slice(2);
	}
	const glitchy = cap.style === 'glitch';
	// tempo: a word every quarter beat; a long line on a short shot tightens so it is all up within ~45% of its time
	const bf = beatFrames(beat, fps);
	const words = words0;
	const fit = Math.floor((total * 0.45) / Math.max(1, words.length));
	const per = kind === 'type' ? 3 : Math.max(2, Math.min(Math.round(bf / 4), fit, 5));
	const lead = cap.enter === 'fade' ? 4 : 0;
	const t0 = (i) => (impact || whip || kind === 'slam' ? 0 : lead + i * per);
	// the marker lands on the first half beat after its word is up (cuts sit on beats, so this is on the grid too)
	const ew = label ? (cap.emph ?? -1) - 2 : cap.emph ?? -1;
	const landed = (ew >= 0 ? t0(ew) : 0) + (impact ? 4 : 2);
	const exitAt = total >= 18 ? total - 5 : total + 99; // short lines just cut with the picture
	const markAt = Math.min(landed + 5, Math.ceil(landed / (bf / 2)) * (bf / 2), exitAt - 8);
	const exitAtWord = (i) => exitAt + i * Math.min(0.6, 3 / Math.max(1, words.length));

	// locked to the picture: the shake track, the zoom punch and the whip come from bot/render.py's own numbers
	const [sx, sy] = (cap.shake || [])[frame] || [0, 0];
	// the renderer's shot-local time on this frame (a shot can start between frames: props.tl0 carries the offset)
	const tl = (f) => (cap.tl0 || 0) + f / fps;
	const zoom = punch ? 1 + 0.2 * Math.exp(-10 * tl(frame)) : 1;
	const wx = whip ? whipX(tl(frame), fps, L.w) : 0;
	const hitAt = cap.hitAt ?? 0; // frames from this caption's start to the rig's impact cue (bot/rig.py): burst + thump
	const slam = (f) => (impact && f >= hitAt ? scaleRamp(f - hitAt, 0, 6, 1.42, 1) : 1);
	const inverted = false; // the renderer's impact is a flash frame now (no inverted ink frame to match)
	// the glitch: always the same shape, decaying over the impact's frames; shake-only lines get rare 1-frame ticks
	const hitGlitch = impact && frame >= hitAt ? [18, 13, 9, 5, 2][frame - hitAt] || 0 : 0;
	const micro = glitchy && !impact && frame > 6 && frame < exitAt && random(`mg-${cap.text}-${frame}`) < 0.06 ? 7 : 0;
	const drift = ramp(frame, 0, total, 5, -5, SWAY);

	const box = {
		position: 'absolute',
		left: gridLeft(st),
		width: L.capW ?? L.w * lay.max_width,
		top: L.capTop ?? L.h * lay.y,
		translate: `0px calc(-50% + ${drift}px)`,
		scale: slam(frame),
		transformOrigin: '0% 50%',
	};
	const textStyle = {
		fontFamily: st.body,
		fontWeight: 700,
		fontSize: L.capSize ?? lay.size,
		lineHeight: `${L.capLine ?? lay.line_height}px`,
		letterSpacing: 0.5,
	};
	return (
		// renderer order: zoom punch first, then shake and whip on top (so those offsets are never scaled)
		<div style={{position: 'absolute', inset: 0, translate: `${wx + sx}px ${sy}px`}}>
			<div style={{position: 'absolute', inset: 0, scale: zoom, transformOrigin: `${L.w / 2}px ${L.h / 2}px`}}>
				<MotionBlur vx={whip ? wx - whipX(tl(frame - 1), fps, L.w) : 0} iso={impact ? Math.abs(slam(frame) - slam(frame - 1)) * 22 : 0} style={box}>
					<Glitch amount={hitGlitch || micro} c={c}>
						{label ? (
							<div style={{fontFamily: st.body, fontWeight: 700, fontSize: 44, letterSpacing: 6, color: c.accent, marginBottom: 10, ...inked(c, 6)}}>{label}</div>
						) : null}
						<div style={textStyle}>
							{words.map((w, i) => (
								<React.Fragment key={i}>
									<CaptionWord
										word={w}
										frame={frame}
										t0={t0(i)}
										markAt={markAt}
										exitAt={exitAtWord(i)}
										emph={i === ew}
										inverted={inverted}
										c={c}
										rise={!impact && !whip && kind !== 'slam' && kind !== 'type'}
									/>
									{i < words.length - 1 ? ' ' : null}
								</React.Fragment>
							))}
						</div>
					</Glitch>
				</MotionBlur>
			</div>
		</div>
	);
};

// ------------------------------------------------------------------------------------------------ outro
// The last calm frame freezes, pushes in and drops out of focus (the dread settles); the line rises word by word in
// the serif on the same grid, and the leading line draws under it.
const Outro = ({text, video, freezeFrame, seconds, beat, st}) => {
	const L = useLayout();
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const c = st.colours;
	const total = Math.max(1, Math.round(seconds * fps));
	const words = text.split(/\s+/).filter(Boolean);
	const gap = Math.max(3, Math.min(5, Math.round(beatFrames(beat, fps) / 4)));
	const ruleAt = 6 + words.length * gap;
	return (
		<AbsoluteFill style={{background: c.ink}}>
			<AbsoluteFill
				style={{
					scale: interpolate(frame, [0, 10, Math.max(11, total)], [1.0, 1.05, 1.1], {...CLAMP, easing: [OUT, SWAY], output: 'perceptual-scale'}),
					filter: `blur(${ramp(frame, 0, 10, 0, 14)}px) brightness(${ramp(frame, 0, 10, 0.9, 0.4)}) saturate(0.7)`,
				}}
			>
				<Freeze frame={freezeFrame}>
					<OffthreadVideo src={staticFile(video)} muted style={{width: '100%', height: '100%', objectFit: 'cover'}} />
				</Freeze>
			</AbsoluteFill>
			<Stage>
				<div
					style={{
						position: 'absolute',
						left: gridLeft(st),
						width: L.outroW,
						top: L.outroTop,
						translate: `0px ${ramp(frame, 0, total, 10, -10, SWAY)}px`,
					}}
				>
					<div style={{fontFamily: st.head, fontSize: 76, lineHeight: 1.25, color: c.paper, ...inked(c, 6)}}>
						{words.map((w, i) => (
							<React.Fragment key={i}>
								<RiseWord word={w} t0={6 + i * gap} exitAt={total + 99} frame={frame} />
								{i < words.length - 1 ? ' ' : null}
							</React.Fragment>
						))}
					</div>
					<EdgeRule st={st} progress={ramp(frame, ruleAt, ruleAt + 12, 0, 1)} top={32} />
				</div>
			</Stage>
		</AbsoluteFill>
	);
};

// ------------------------------------------------------------------------------------------------ end card
// The frame everything resolves into, designed first: the brand locked on the grid, off-centre, in open space.
// The leading line draws in from the edge, the title rides in after its tip letter by letter (rise + de-blur), the
// call to action wipes in under it, the studio name comes last. Then it holds, breathing very slightly.
// The Shorts' end tag (props.endCard.style "tag"): no black card. The brand rides the top of the last shot for ~1.3 s
// (the line draws in, the letters land, the call to action wipes in), so the Short ends on a strong frame and loops.
const EndTag = ({title, cta, letterFrames, st}) => {
	const L = useLayout();
	const frame = useCurrentFrame();
	const c = st.colours;
	return (
		<Stage>
			<div style={{position: 'absolute', left: gridLeft(st), top: L.tagTop, width: L.w - 2 * gridLeft(st)}}>
				<EdgeRule st={st} progress={ramp(frame, 0, 8, 0, 1)} reach={160} />
				<div style={{marginTop: 22, fontFamily: st.head, fontSize: 172, lineHeight: 1.0, color: c.paper, ...inked(c, 9)}}>
					{Array.from(title).map((ch, i) => {
						const t0 = letterFrames?.[i] ?? 3 + i;
						const y = (f) => ramp(f - t0, 0, 9, 26, 0);
						return (
							<span key={i} style={{display: 'inline-block', visibility: frame < t0 ? 'hidden' : 'visible', translate: `0px ${y(frame)}px`}}>
								{ch}
							</span>
						);
					})}
				</div>
				<div style={{marginTop: 22, fontFamily: st.body, fontWeight: 700, fontSize: 76, letterSpacing: 3, color: c.paper, ...inked(c, 9), clipPath: `inset(-30% ${ramp(frame, 8, 18, 100, 0)}% -30% -5%)`}}>
					{cta}
				</div>
			</div>
		</Stage>
	);
};

const EndCard = ({title, cta, byline, letterFrames, st}) => {
	const L = useLayout();
	const frame = useCurrentFrame();
	const {durationInFrames} = useVideoConfig();
	const c = st.colours;
	const letters = Array.from(title);
	const track = ramp(frame, 2, 26, 0.3, 0.02);
	// the long video's end screen holds ~9 s: it keeps breathing the whole time (never a frozen frame)
	const breathe = L.w > L.h ? ramp(frame, 0, Math.max(61, durationInFrames), 1, 1.035, SWAY) : ramp(frame, 0, 60, 1, 1.02, SWAY);
	return (
		<AbsoluteFill style={{background: c.ink}}>
			<Stage>
				<div style={{position: 'absolute', left: gridLeft(st), top: L.endTop, width: L.endW, scale: breathe, transformOrigin: '0% 50%'}}>
					<EdgeRule st={st} progress={ramp(frame, 0, 10, 0, 1)} reach={160} />
					<div style={{marginTop: 34, fontFamily: st.head, fontSize: 150, lineHeight: 1.05, color: c.paper, letterSpacing: `${track}em`}}>
						{letters.map((ch, i) => {
							const t0 = letterFrames?.[i] ?? 3 + i; // the rig's frames: each one has its tick
							const local = frame - t0;
							const y = (f) => ramp(f - t0, 0, 11, 30, 0);
							return (
								<MotionBlur
									key={i}
									vy={y(frame) - y(frame - 1)}
									iso={ramp(local, 0, 8, 10, 0)}
									style={{display: 'inline-block', visibility: local < 0 ? 'hidden' : 'visible', translate: `0px ${y(frame)}px`}}
								>
									{ch}
								</MotionBlur>
							);
						})}
					</div>
					<div
						style={{
							marginTop: 26,
							fontFamily: st.body,
							fontWeight: 700,
							fontSize: 42,
							letterSpacing: 6,
							color: c.paper,
							clipPath: `inset(-20% ${ramp(frame, 12, 24, 100, 0)}% -20% -5%)`,
						}}
					>
						{cta}
					</div>
					{byline ? (
						<div
							style={{
								marginTop: 64,
								fontFamily: st.body,
								fontWeight: 500,
								fontSize: 28,
								color: c.sub,
								letterSpacing: ramp(frame, 18, 34, 20, 12),
								visibility: frame < 18 ? 'hidden' : 'visible',
								translate: `0px ${ramp(frame, 18, 28, 14, 0)}px`,
							}}
						>
							{byline}
						</div>
					) : null}
				</div>
			</Stage>
		</AbsoluteFill>
	);
};

// ------------------------------------------------------------------------------------------------ chapter card
// The long video's chapter title, on the same grid: the leading line draws in from the edge with the chapter number
// riding its tip, the dream's name rises word by word in the serif (each word on the rig's frame, with its tick), the
// dream's one-line detail wipes in under it. Exit: everything lifts out of its mask on the cut.
const ChapterCard = ({ch, seconds, st}) => {
	const L = useLayout();
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const c = st.colours;
	const total = Math.max(8, Math.round(seconds * fps));
	const exitAt = total - 6;
	const words = ch.name.split(/\s+/).filter(Boolean);
	const lastWord = (ch.wordFrames?.[words.length - 1] ?? 8 + 3 * (words.length - 1)) + 6;
	const out = ramp(frame, exitAt, exitAt + 5, 0, 100, IN);
	const label = `chapter ${String(ch.n).padStart(2, '0')}`;
	return (
		<div style={{position: 'absolute', left: gridLeft(st), top: L.chapterTop ?? 600, width: L.w - gridLeft(st) * 2, translate: `0px ${ramp(frame, 0, total, -5, 5, SWAY)}px`}}>
			<div
				style={{
					fontFamily: st.body,
					fontWeight: 700,
					fontSize: 30,
					letterSpacing: ramp(frame, 0, 14, 14, 8),
					color: c.accent,
					...inked(c, 6),
					clipPath: `inset(-20% ${ramp(frame, 2, 10, 100, 0)}% ${out}% -5%)`,
				}}
			>
				{label}
			</div>
			<EdgeRule st={st} progress={ramp(frame, 0, 9, 0, 1) * ramp(frame, exitAt, exitAt + 4, 1, 0, IN)} reach={200} top={18} />
			<div style={{marginTop: 26, fontFamily: st.head, fontSize: 118, lineHeight: 1.08, color: c.paper, ...inked(c, 8)}}>
				{/* edit v4: each word of the dream's name arrives stretched wide and tall and pulls together on a spring
				    (de-blurring as it settles), on the rig's word frames (each has its tick) */}
				{words.map((w, i) => {
					const t0 = ch.wordFrames?.[i] ?? 8 + 3 * i;
					return (
						<React.Fragment key={i}>
							<span style={{display: 'inline-block'}}>
								<StretchLine text={w} frame={frame - t0 + 1} fps={fps} exitAt={exitAt + Math.min(i, 3) * 0.5 - t0 + 1} />
							</span>
							{i < words.length - 1 ? ' ' : null}
						</React.Fragment>
					);
				})}
			</div>
			{ch.line ? (
				<div
					style={{
						marginTop: 30,
						maxWidth: 1100,
						fontFamily: st.body,
						fontWeight: 500,
						fontSize: 38,
						lineHeight: 1.3,
						color: c.sub,
						clipPath: `inset(-20% ${ramp(frame, lastWord, lastWord + 12, 100, 0)}% ${out}% -5%)`,
					}}
				>
					{ch.line}
				</div>
			) : null}
		</div>
	);
};

// The layout's stage (1080x1920 or 1920x1080) scaled to the real composition size.
const Stage = ({children}) => {
	const L = useLayout();
	const {width} = useVideoConfig();
	const k = width / L.w;
	return (
		<div style={{position: 'absolute', left: 0, top: 0, width: L.w, height: L.h, transform: `scale(${k})`, transformOrigin: '0 0'}}>
			{children}
		</div>
	);
};

export const Short = (props) => {
	const st = props.style || FALLBACK_STYLE;
	useFonts(st.fonts);
	const {fps, durationInFrames} = useVideoConfig();
	const f = (s) => Math.round(s * fps);
	const {video, hook, captions, outro, endCard, freezeAt} = props;
	const L = LAYOUTS[props.layout] || LAYOUTS.portrait;
	const endFrom = durationInFrames - f(endCard.seconds);
	const filmFrom = Math.max(0, props.filmFrom ?? 0);

	return (
		<LayoutCtx.Provider value={L}>
		<AbsoluteFill style={props.transparent ? undefined : {background: st.colours.ink}}>
			{/* the clean background; audio is muxed back from the same file by the bot. props.transparent (the long
			    video's text-only ranges): no video at all, the frames carry an alpha channel and ffmpeg lays them
			    over the clean video. Nothing in a text-only range blends with the picture (the grain, which does,
			    is only in the opaque tail ranges). */}
			{props.transparent ? null : (
				<OffthreadVideo src={staticFile(video)} muted style={{width: '100%', height: '100%', objectFit: 'cover'}} />
			)}

			<Stage>
				{/* particles first: text always draws over them (and they are clipped clear of it anyway) */}
				<Particles cues={props.cues || []} until={endFrom} c={st.colours} layout={props.layout} dust={props.dust !== false} low={endCard.style === 'tag'} />
				{(props.chapters || []).map((ch, i) => (
					<Sequence key={`ch${i}`} from={f(ch.start)} durationInFrames={Math.max(1, f(ch.start + ch.seconds) - f(ch.start))} layout="none">
						<ChapterCard ch={ch} seconds={ch.seconds} st={st} />
					</Sequence>
				))}
				{captions.map((c, i) => {
					const from = f(c.start);
					const dur = Math.min(f(c.end), endCard.style === 'tag' ? durationInFrames : endFrom) - from;
					if (dur <= 0) return null;
					return (
						<Sequence key={i} from={from} durationInFrames={dur} layout="none">
							<Caption cap={c} seconds={dur / fps} beat={props.beat} st={st} />
						</Sequence>
					);
				})}
				{hook ? (
					<Sequence from={0} durationInFrames={f(hook.until)} layout="none">
						<Hook title={hook.title} label={hook.label} seconds={hook.until} beat={props.beat} wordFrames={hook.wordFrames} st={st} kind={hook.kind} />
					</Sequence>
				) : null}
			</Stage>

			{outro && f(outro.start) < endFrom ? (
				<Sequence from={f(outro.start)} durationInFrames={endFrom - f(outro.start)}>
					<Outro text={outro.text} video={video} freezeFrame={f(freezeAt)} seconds={(endFrom - f(outro.start)) / fps} beat={props.beat} st={st} />
				</Sequence>
			) : null}

			<Sequence from={endFrom}>
				{endCard.style === 'tag' ? (
					<EndTag title={endCard.title} cta={endCard.cta} letterFrames={endCard.letterFrames} st={st} />
				) : (
					<EndCard title={endCard.title} cta={endCard.cta} byline={endCard.byline} letterFrames={endCard.letterFrames} st={st} />
				)}
			</Sequence>

			{/* the long video renders only its text ranges, so its film texture starts at the outro (no seams) */}
			<Sequence from={filmFrom} layout="none">
				<FilmTexture until={endFrom - filmFrom} c={st.colours} light={props.graded === true} />
			</Sequence>
		</AbsoluteFill>
		</LayoutCtx.Provider>
	);
};
