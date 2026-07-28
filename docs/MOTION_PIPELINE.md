# Motion Pipeline Contract

Motion generation is a gated source-to-runtime pipeline. A good source is not permission to publish an alpha asset, and a passing Idle is not permission to publish Walk. Each stage must preserve the evidence needed to reject the next stage without modifying official runtime assets.

## Idle lesson: alpha and color are separate

The approved Idle source had valid anatomy and color. The failure was introduced locally: repeated green removal and temporal edge-color propagation changed opaque subject RGB around the left temple, hairline, black pumps, thin heels, and shoe highlights. The silhouette could look complete while the subject color was already damaged.

The following invariants apply identically to Idle and Walk:

1. Chroma keying computes alpha only. It does not recolor opaque subject pixels.
2. Temporal repair aligns neighbouring frames before recovering alpha. Corresponding RGB is restored only for pixels whose alpha was actually recovered.
3. Green despill is limited to genuinely green-dominant pixels. Skin, lips, fuchsia fabric, black leather, and neutral highlights are protected.
4. Edge decontamination runs once, at the final edge-cleanup stage, and only on semi-transparent pixels (`alpha < 246`). Opaque hair strands, shoe edges, and highlights retain source RGB.
5. RGBA resizing uses premultiplied alpha. Straight RGB resizing can pull transparent green into hair and footwear edges.
6. A source-versus-output color gate must pass: protected opaque-channel delta p99 `<= 2`, protected pixels over delta 12 `<= 0.0005`, and residual green p99 `<= 6`.
7. Review alpha results against contrasting backgrounds and inspect face/hair and both shoes at high magnification. A contact sheet alone is not enough for thin heels or temple edges.

Never compensate for absent anatomy by expanding alpha, copying a whole limb from another frame, or propagating colour across an opaque edge. If a hand, heel, hair edge, or shoe is not present in the source, reject and regenerate the source.

## Reprocessing an approved original source

If the user approves an original I2V motion, do not regenerate it merely because a stricter tracker would now reject a hidden or low-confidence joint. Preserve the approved contiguous frames, timing, travel trajectory, pose, hair, and gait.

When a separate green-screen background edit exists, treat it as an alpha-matte source only. Track both videos, fit a per-frame similarity transform from at least four shared body joints, warp only the green-derived alpha into the original frame, and keep the original I2V RGB as colour authority. Require silhouette agreement against an independent original-source segmentation: IoU p10 `>= 0.88` and minimum `>= 0.85`. Constrain the aligned matte to a one-pixel dilation of that original silhouette, keep every non-trusted boundary below opaque alpha, and use original-derived decontaminated boundary RGB before the single final edge cleanup. Then apply premultiplied resize and an all-opaque source-RGB delta gate. Do not run green despill or green-residual rejection on an approved non-green original: legitimate green-dominant source pixels are colour-authoritative too. Never paste unaligned alpha or let the background-edit render overwrite approved subject RGB.

## Source authority and approval order

Image-generation references must have explicit roles. The original portrait is the face and hairstyle authority; a full-body reference may supply proportions and wardrobe only. Prompts must name hairstyle geometry, not merely say “same hair.” For Vivieen Walk this means a deep side part, smooth crown volume, broad controlled dark-brunette S-waves, and one side tucked behind the ear—not a centre part, beach curls, lengthened hair, ponytail, or chignon.

Approval is sequential:

1. Approve the motion keyframe.
2. Approve the raw green-screen video.
3. Approve the transparent loop and magnified edge proofs.
4. Approve bidirectional playback and the full-frame checklist.
5. Only then replace official motion assets, install the app, or publish a release.

A rejected stage is retained as evidence but is never promoted as a candidate. Interpolation may bridge at most two missing pose samples for measurement; it must never make missing anatomy eligible for publication.

## Horizon Walk inherits Idle, then adds traversal gates

Every Horizon Walk style uses the exact hue-safe alpha/color path above. Styles change motion direction and validation ranges; they never select a separate or more aggressive keyer.

Universal gates:

- Camera registration lines remain fixed and the camera, exposure, color, identity, hair, wardrobe, and footwear stay locked.
- The selected loop must close visually and preserve both hands, both ankles, both shoes, and both thin heels in every runtime frame.
- The source root must travel steadily from camera-left to camera-right with a valid continuous trajectory; estimated desktop speed is not substituted for a missing trajectory.
- Each tracked extremity is measured independently. Missing anatomy, invalid loop closure, color drift, or a broken traversal rejects the candidate for every style.

Office and stylized gait profiles additionally require bilateral gait evidence: each arm and leg completes its excursion, pose and velocity close at the seam, and arm/leg motion remains contralateral. Office walk keeps the strict original limits: wrist elevation p90 `<= 0.10` and maximum `<= 0.16`, swing-foot lift p90 `<= 0.16` and maximum `<= 0.22`, and minimum arm excursion `0.025` of tracked body height. Natural office gait outranks “show both arms”; visibility comes from the stable three-quarter torso angle, never raised or spread arms.

Runway catwalk, Relaxed stroll, Brisk power walk, and Elegant promenade use the stylized-gait profile: wrist elevation p90 `<= 0.22` and maximum `<= 0.32`, swing-foot lift p90 `<= 0.24` and maximum `<= 0.34`, and minimum arm excursion `0.020`. These broader ranges permit the selected style without weakening bilateral closure, contralateral coordination, anatomy, extremity, identity, color, or trajectory checks.

Cartwheel is a traversal rather than a gait cycle. It must begin and finish upright, complete one lateral hand-supported inversion, preserve full wardrobe coverage and every extremity, close cleanly, and maintain valid left-to-right root travel. Office arm-swing and foot-lift constraints do not apply to an inverted traversal.

If any applicable gate fails, regenerate from the last approved source authority. Do not lower thresholds, select a prettier but invalid segment, or let user approval of one stage silently approve later stages.
