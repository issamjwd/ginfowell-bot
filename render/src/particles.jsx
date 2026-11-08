import React from 'react';
import {Sequence, random, useCurrentFrame} from 'remotion';

// Particles, the free stand-in for After Effects + Trapcode Particular. Deterministic: every value comes from
// remotion's seeded random(), keyed by the cue frame and the particle index, so a re-render is identical frame for
// frame. Timing comes ONLY from props.cues (bot/rig.py), the same sheet the picture's impact/shake, the caption slam
// and the sound effects read, so a burst, its shake and its thump land on the same frame.
// House rules: solid shapes, one accent, no glow, no gradients, no opacity fades (things appear and leave by moving
// and shrinking). Everything stays inside SAFE: below the caption/hook band, left of the Shorts button column and
// above the bottom title area. The layer is also hard-clipped to SAFE, so nothing can ever cover text or buttons.

// 1080x1920 stage px. Captions sit around y 480 (upper third, <= ~650 on a 3-line slam); the hook ends ~640;
// YouTube's right-hand buttons start near x 940; its title/channel overlay covers the bottom ~22% (y > ~1500).
export const SAFE = {x0: 24, y0: 720, x1: 900, y1: 1460};
// the burst's origin sits on the speed lines' focus (frame centre), nudged down into SAFE
const BURST = {x: 500, y: 1040, count: 22, frames: 24, drag: 0.11, gravity: 0.7};

const smooth = (x) => {
	const t = Math.min(1, Math.max(0, x));
	return t * t * (3 - 2 * t);
};
// a hump that peaks at t = tau and settles back (an impulse for the dust)
const hump = (t, tau) => (t <= 0 ? 0 : (t / tau) * Math.exp(1 - t / tau));

// One impact: shards thrown out from the frame's centre of action with drag and a little gravity. Each shard is a
// streak stretched along its velocity that shortens as it slows, then shrinks to nothing. It rides the picture's own
// shake (cue.shake, the renderer's numbers) and flips colour on the renderer's inverted frame (local frame 1).
const Shard = ({seed, i, t, c}) => {
	const r = (k) => random(`${seed}-${i}-${k}`);
	const angle = r(0) * Math.PI * 2;
	const v = 30 + r(1) * 26; // px/frame; with drag 0.11 a shard travels at most v / 0.11 (270..510 px)
	const {drag: k, gravity: g} = BURST;
	const dist = (tt) => (v * (1 - Math.exp(-k * tt))) / k;
	const x = Math.cos(angle) * dist(t);
	const y = Math.sin(angle) * dist(t) + 0.5 * g * t * t;
	const vx = Math.cos(angle) * v * Math.exp(-k * t);
	const vy = Math.sin(angle) * v * Math.exp(-k * t) + g * t;
	const speed = Math.hypot(vx, vy);
	const life = 14 + r(2) * 10;
	const size = (12 + r(3) * 16) * (1 - smooth(t / life));
	if (size < 0.5) return null;
	const accent = i % 9 === 0;
	const darkShard = r(4) < 0.45; // ink and paper shards: one of them always reads on pale or dark footage
	const inverted = Math.round(t) === 1;
	const colour = accent ? c.accent : darkShard !== inverted ? c.ink : c.paper;
	return (
		<div
			style={{
				position: 'absolute',
				left: BURST.x + x,
				top: BURST.y + y,
				width: size * (1 + Math.min(4, speed * 0.12)),
				height: size * 0.5,
				background: colour,
				borderRadius: 1,
				translate: '-50% -50%',
				rotate: `${Math.atan2(vy, vx)}rad`,
			}}
		/>
	);
};

const Burst = ({cue, c}) => {
	const t = useCurrentFrame();
	const [sx, sy] = (cue.shake || [])[t] || [0, 0];
	return (
		<div style={{position: 'absolute', inset: 0, translate: `${sx}px ${sy}px`}}>
			{Array.from({length: BURST.count}, (_, i) => (
				<Shard key={i} seed={`burst-${cue.f}`} i={i} t={t} c={c} />
			))}
		</div>
	);
};

// Drifting dust: a few motes rise slowly through SAFE with a lazy sway, growing in from nothing at the bottom and
// shrinking away at the top. Each impact cue kicks them outward from the burst and lets them settle back.
const DUST = 14;
const Dust = ({cues, until, c}) => {
	const frame = useCurrentFrame();
	if (frame >= until) return null;
	const impacts = cues.filter((q) => q.kind === 'impact' && q.f <= frame);
	const inverted = impacts.some((q) => frame - q.f === 1);
	const h = SAFE.y1 - SAFE.y0;
	return (
		<>
			{Array.from({length: DUST}, (_, i) => {
				const r = (k) => random(`dust-${i}-${k}`);
				const period = 480 + r(1) * 360;
				const u = (frame / period + r(2)) % 1;
				let x = SAFE.x0 + 30 + r(0) * (SAFE.x1 - SAFE.x0 - 60) + 18 * Math.sin((frame / (90 + r(3) * 60)) * Math.PI * 2 + r(4) * 6.28);
				let y = SAFE.y1 - u * h;
				for (const q of impacts) {
					const dx = x - BURST.x;
					const dy = y - BURST.y;
					const d = Math.max(40, Math.hypot(dx, dy));
					const push = hump(frame - q.f, 4) * 70 * Math.min(1, 260 / d);
					x += (dx / d) * push;
					y += (dy / d) * push;
				}
				const size = (5 + r(5) * 5) * Math.sin(Math.PI * u);
				if (size < 0.5) return null;
				return (
					<div
						key={i}
						style={{
							position: 'absolute',
							left: x,
							top: y,
							width: size,
							height: size,
							borderRadius: '50%',
							background: inverted ? c.ink : c.paper,
							opacity: 0.8,
							translate: '-50% -50%',
						}}
					/>
				);
			})}
		</>
	);
};

// The long video (1920x1080 stage, props.layout "landscape"): no Shorts buttons, captions in the upper-left column, so
// SAFE is the lower band of the whole frame and the burst sits on the frame centre (moved there by an offset).
export const SAFE_LANDSCAPE = {x0: 24, y0: 470, x1: 1896, y1: 1056};
const LANDSCAPE_SHIFT = {x: 960 - BURST.x, y: 720 - BURST.y};

// The edit styles' Shorts (props.layout "portrait-low"): the burst comes from low in the frame, under where faces sit
// (the monster's head is usually mid-frame), so it never covers the subject.
const LOW_SHIFT = {x: 0, y: 300};

export const Particles = ({cues, until, c, layout = 'portrait', dust = true, low = false}) => {
	const wide = layout === 'landscape';
	const safe = wide ? SAFE_LANDSCAPE : SAFE;
	const [W, H] = wide ? [1920, 1080] : [1080, 1920];
	const shift = wide ? LANDSCAPE_SHIFT : low ? LOW_SHIFT : {x: 0, y: 0};
	return (
		<div
			style={{
				position: 'absolute',
				inset: 0,
				pointerEvents: 'none',
				clipPath: `inset(${safe.y0}px ${W - safe.x1}px ${H - safe.y1}px ${safe.x0}px)`,
			}}
		>
			{dust ? <Dust cues={cues} until={until} c={c} /> : null}
			<div style={{position: 'absolute', inset: 0, translate: `${shift.x}px ${shift.y}px`}}>
				{cues
					.filter((q) => q.kind === 'impact' && q.burst !== false && q.f < until)
					.map((q, i) => (
						<Sequence key={i} from={q.f} durationInFrames={Math.min(BURST.frames, until - q.f)} layout="none">
							<Burst cue={q} c={c} />
						</Sequence>
					))}
			</div>
		</div>
	);
};
