// LUC3D's Track All grouping of a session, for bench/pose3d_validate.py: LUC3D's own
// CrossViewTracker (pose/cross-view-tracker.js, imported read-only from a LUC3D clone)
// over every camera, with the Tracking Wizard's defaults, maxTargets = the animal count
// and every node weight 1, cameras in calibration order.
//
//   node bench/pose3d_luc3d_ref.mjs <folder> <animals> <luc3d clone>
//
// <folder> holds meta.json ({shape: [F, C, I, N, 2], cams: [{name, matrix, dist, rvec,
// tvec, size}]}), xy.f64 (the keypoints, NaN = missing) and count.i32 (F x C detections).
// Writes <folder>/luc3d_ref.json: per frame, per target holding detections from >= 2
// cameras that frame (what Track All commits), [trackId, slot in each camera or -1].
import fs from 'fs';
import path from 'path';
import { pathToFileURL } from 'url';

const [dir, nAnimals, luc3d] = [process.argv[2], parseInt(process.argv[3]), process.argv[4]];
const pose = (f) => pathToFileURL(path.join(luc3d, 'pose', f)).href;
const { Camera, Instance } = await import(pose('pose-data.js'));
const { CrossViewTracker, Detection } = await import(pose('cross-view-tracker.js'));

const meta = JSON.parse(fs.readFileSync(path.join(dir, 'meta.json')));
const [F, C, I, N] = meta.shape;
const xy = new Float64Array(fs.readFileSync(path.join(dir, 'xy.f64')).buffer.slice(0));
const count = new Int32Array(fs.readFileSync(path.join(dir, 'count.i32')).buffer.slice(0));
const cams = meta.cams.map(c => new Camera(c.name, c.matrix, c.dist, c.rvec, c.tvec, c.size));
const camIndex = new Map(cams.map((c, i) => [c.name, i]));
// ui/settings.js defaults (TRACKING_THRESHOLDS)
const trk = new CrossViewTracker({
    corr2dWeight: 1, corr3dWeight: 6, velocityThreshold: 10, distanceThreshold: 25, timePenalty: 0.1,
    stale: 20, matchGate: 0, maxTargets: nAnimals, nodeWeights: new Array(N).fill(1),
});
const out = [];
const t0 = performance.now();
for (let f = 0; f < F; f++) {
    const detsByCam = new Map();
    for (let c = 0; c < C; c++) {
        const n = count[f * C + c], dets = [];
        for (let s = 0; s < n; s++) {
            const off = ((f * C + c) * I + s) * N * 2;
            dets.push(new Detection(new Instance(xy.slice(off, off + 2 * N), 0, 'predicted', 1), cams[c], f, s));
        }
        detsByCam.set(cams[c].name, dets);
    }
    trk.trackFrame(detsByCam, cams);
    const rows = [];
    for (const t of trk.targets) {
        const slots = new Array(C).fill(-1);
        let k = 0;
        t.detsByCam.forEach((d, name) => { if (d.frameIdx === f) { slots[camIndex.get(name)] = d.slot; k++; } });
        if (k >= 2) rows.push([t.trackId, ...slots]);
    }
    out.push(rows);
}
console.error(`LUC3D tracked ${F} frames of ${C} cameras in ${((performance.now() - t0) / 1000).toFixed(1)} s, ${trk.targets.length} targets`);
fs.writeFileSync(path.join(dir, 'luc3d_ref.json'), JSON.stringify(out));
