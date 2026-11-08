import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {Short} from './Short';

// Props come from bot/overlay.py (props_from_storyboard) via render.mjs, which also adds `style` from
// assets/style.json. These defaults only exist so the composition opens in Remotion Studio.
const defaultProps = {
	video: 'bg.mp4',
	width: 1080,
	height: 1920,
	fps: 24,
	durationInFrames: 240,
	hook: {title: 'the hills', label: '', until: 2.0},
	captions: [],
	outro: null,
	endCard: {title: 'liminality', cta: 'free on roblox', byline: 'PIXELHELM', seconds: 1.5},
	freezeAt: 6,
	style: null,
};

const Root = () => (
	<Composition
		id="LiminalityShort"
		component={Short}
		fps={24}
		width={1080}
		height={1920}
		durationInFrames={defaultProps.durationInFrames}
		defaultProps={defaultProps}
		calculateMetadata={({props}) => ({
			fps: props.fps,
			width: props.width,
			height: props.height,
			durationInFrames: Math.max(1, props.durationInFrames),
		})}
	/>
);

registerRoot(Root);
