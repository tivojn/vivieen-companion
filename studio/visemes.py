"""Viseme catalog + prompt construction.

Three lessons are baked into these prompts:

1.  POSE.  gpt-image-2 draws mouths on a FRONTAL prior, so a turned keyframe
    yields mouths in the wrong perspective.  Build avatars from a front-facing
    source; `pose_clause()` states the measured pose so a turned one degrades
    gracefully.

2.  PLACE OF ARTICULATION.  Naming only the letter produced tongue errors - the
    model drew a TH/L tongue for alveolar D and N.  Every prompt names the
    articulator position AND names the wrong shape to exclude.  Naming the shape
    to exclude is what stopped it.

3.  AMPLITUDE.  Textbook articulation descriptions describe CITATION FORM - a
    phoneme pronounced in isolation for a phonetics class.  Fed to an image
    model they produce a face that is shouting: the first pass measured an AH
    aperture of 0.56 of mouth width, where relaxed conversation is ~0.22, and an
    FF that bared teeth like a snarl instead of gently tucking the lip.  So the
    amplitude governor below is stated up front, and every shape carries an
    explicit opening measured against a feature the model can actually see -
    the thickness of the subject's own lower lip.  `TARGETS` holds the same
    numbers for the QA pass, so the prompt and the verifier cannot drift apart.
"""

BASE = (
    "Edit this portrait photograph. This is a LIP-SYNC MOUTH SHAPE (viseme) frame "
    "for a talking-head animation, so the ONLY thing that may change is the mouth "
    "and jaw.\n\n"
    "ABSOLUTELY UNCHANGED, pixel for pixel: the person's identity and facial bone "
    "structure, head position, head angle, head size, camera framing and distance, "
    "crop, eyes, eyebrows, eyelids, gaze direction, nose, ears, hairline, hair, "
    "jewellery, clothing, neckline, shoulders, skin tone, freckles, makeup, "
    "lighting direction, shadows, colour grade and background.\n\n"
    "DO NOT re-frame, re-crop, zoom, rotate, re-pose, re-light or re-render the "
    "image. DO NOT beautify, smooth or retouch the skin. DO NOT change her "
    "expression beyond the mouth. Keep photographic realism with visible skin "
    "texture and pores.\n\n"
)

AMPLITUDE = (
    "AMPLITUDE - THE SINGLE MOST IMPORTANT CONSTRAINT:\n"
    "This is QUIET CONVERSATIONAL SPEECH at close range - the small, efficient, "
    "almost lazy mouth movement of a composed adult talking calmly to one person "
    "sitting across a desk. It is NOT singing, NOT shouting, NOT calling out, NOT "
    "stage or theatrical diction, and NOT an exaggerated phonetics-textbook diagram "
    "of the sound. The jaw barely moves. The lips move a few millimetres, not "
    "centimetres. Her expression stays calm and composed throughout - no grimace, "
    "no snarl, no baring of the teeth, no strain in the cheeks or chin.\n"
    "Keep the change SMALL and UNDERSTATED. If you are unsure, make it SMALLER. An "
    "over-articulated mouth looks like the person is yelling and is a failure.\n\n"
    "BUT THERE IS A FLOOR - small does not mean closed. Unless this shape is "
    "explicitly a closed-lip shape, the mouth must stay clearly DISTINGUISHABLE "
    "from the closed resting mouth: its defining feature - the gap, the teeth "
    "contact, the rounding, the slit - must be visible at a glance. Shrink the "
    "movement; never delete it.\n\n"
)

DENTAL_CONTINUITY = (
    "DENTAL CONTINUITY - FIXED ANATOMY:\n"
    "Her upper teeth are one rigid part of her skull. Across every mouth shape, keep "
    "the SAME upper dental row: identical tooth count, incisor widths, spacing, edge "
    "contour, colour and screen position. The upper teeth must never slide, scale, "
    "tilt or regenerate with the lips. Only the lips and jaw change how much of that "
    "one fixed row is revealed. Her lower teeth belong to the moving jaw: whenever "
    "the opening naturally reveals their incisal edge, preserve that subtle lower row "
    "instead of hiding it or replacing it with shadow. Do not force lower teeth into "
    "closed-lip shapes and do not invent extra teeth.\n\n"
)

ORAL_RENDERING = (
    "ORAL SHADING - NATURAL, SOFT AND LOW-CONTRAST:\n"
    "Do not draw or trace a dark line along the inner lip contour. Do not add black "
    "outlines around the lips, teeth or gums. Define every boundary with soft natural "
    "tonal transitions, not ink-like edges. The visible oral interior is softly lit "
    "warm rose or muted burgundy, never black, charcoal or a flat dark fill. Keep any "
    "rear-mouth shadow small and graduated. When both dental rows are visible, their "
    "meeting slit is a soft low-contrast occlusion, never a dark horizontal stripe. "
    "Keep visible lower incisors clean and naturally ivory rather than merging them "
    "into the cavity shadow.\n\n"
)

CLOSER = ("\n\nRender only that mouth change, at the small conversational scale "
          "described above. Everything else in the frame must be identical to the "
          "input image.")

# name -> (phoneme group, articulation, opening spec)
# The opening is expressed against the subject's own lower lip thickness, which
# is a feature the model can see in the reference; absolute units mean nothing to it.
SHAPES = {
 "closed": ("rest / silence",
    "Lips gently and naturally CLOSED in a relaxed neutral rest position, a soft "
    "natural lip seam. Jaw closed. No teeth and no tongue visible. The lips must NOT "
    "be pressed hard or compressed - that is the P/B/M shape, not this one.",
    "No gap at all between the lips."),

 "PP": ("p, b, m",
    "Lips closed together for the consonant P/B/M, the lip line very slightly "
    "compressed and a touch flatter than at rest. Jaw closed. No teeth, no tongue, "
    "no opening whatsoever.",
    "No gap at all. The compression is SUBTLE - just perceptibly firmer than the "
    "resting mouth, with no hard tension ridge, no white pressure marks and no "
    "rolling of the lips inward."),

 "FF": ("f, v",
    "Labiodental F/V: the LOWER LIP comes up to rest LIGHTLY AGAINST THE EDGE OF THE "
    "UPPER FRONT TEETH. Only the very bottom edge of the upper front teeth touches the "
    "lower lip - a soft contact, as in the quiet 'f' of the word 'often'. The tongue is "
    "NOT visible. The lips must NOT be rounded.",
    "The visible gap is a HAIRLINE - about one tenth of her lower lip's thickness. "
    "CRITICAL: the mouth must NOT be pulled wide, the upper lip must NOT curl or lift "
    "to expose the gum line, and the teeth must NOT be bared. This is a soft, almost "
    "closed mouth - it must not look like a snarl, a sneer or an angry word."),

 "TH": ("th",
    "Interdental TH: the very TIP OF THE TONGUE shows as a small flat pink sliver "
    "BETWEEN THE UPPER AND LOWER FRONT TEETH, only just reaching the lip line. This is "
    "the ONLY shape where the tongue reaches the lips. The tongue must stay centred, "
    "must NOT loll out onto the lower lip and must NOT curl to either side.",
    "The lips part by about ONE THIRD of her lower lip's thickness - a small gap. Only "
    "a sliver of tongue shows; the jaw stays nearly closed."),

 "DD": ("d, t",
    "Alveolar consonant D/T: the TONGUE TIP IS PRESSED UP AGAINST THE RIDGE BEHIND THE "
    "UPPER FRONT TEETH so the tongue is HIDDEN INSIDE THE MOUTH. A small softly "
    "shadowed warm gap and the edge of the upper teeth show. The tongue must NOT "
    "protrude, must NOT rest on "
    "the lower lip, must NOT cross the lip line and must NOT lean to either side. This "
    "is NOT a TH shape and NOT an L shape.",
    "The lips part by about ONE THIRD of her lower lip's thickness. Jaw almost closed."),

 "nn": ("n, l",
    "Alveolar nasal N: the TONGUE TIP IS PRESSED UP ON THE RIDGE BEHIND THE UPPER FRONT "
    "TEETH and is HIDDEN INSIDE THE MOUTH. The tongue must NOT protrude past the teeth, "
    "must NOT rest on the lower lip and must NOT curl sideways. NOT a TH shape, NOT an L.",
    "The lips are barely parted - about one fifth of her lower lip's thickness, just "
    "enough to read as open. Jaw effectively closed."),

 "kk": ("k, g",
    "Velar K/G: the BACK of the tongue rises toward the soft palate deep inside the "
    "mouth while the TONGUE TIP RESTS LOW BEHIND THE LOWER FRONT TEETH and is not "
    "visible at the lip line. Lips neutral, neither spread nor rounded.",
    "The lips part by a little under HALF her lower lip's thickness - a small relaxed "
    "oval. The jaw must NOT drop."),

 "CH": ("ch, j, sh",
    "Postalveolar CH/SH/J: lips eased slightly FORWARD into a small soft rounded "
    "opening, teeth close together behind them, the tongue bunched high and completely "
    "hidden. The lips must NOT be spread sideways.",
    "The lips part by about ONE FIFTH of her lower lip's thickness. The forward push is "
    "SLIGHT - the mouth stays about nine tenths of its normal width. Do NOT pucker "
    "strongly and do NOT drop the jaw."),

 "SS": ("s, z",
    "Sibilant S/Z: the TEETH ARE ALMOST TOUCHING behind lips that are only just parted, "
    "showing a NARROW HORIZONTAL SLIT with the upper and lower front teeth nearly "
    "together behind it. The tongue is completely HIDDEN behind the teeth.",
    "The gap is a HAIRLINE - about one tenth of her lower lip's thickness. The jaw must "
    "NOT drop, the lips must NOT stretch wide, and NO tongue may be visible."),

 "RR": ("r",
    "American R: lips very slightly rounded with a small opening, corners drawn a little "
    "inward, the tongue BUNCHED AND RETRACTED in the middle of the mouth, touching "
    "nothing and not visible. This must NOT be a round OH shape and must NOT be a tight "
    "kiss-like OO.",
    "The lips part by about ONE FIFTH of her lower lip's thickness. The rounding is "
    "gentle - the mouth stays about nine tenths of its normal width."),

 "ah": ("a as in father",
    "Open vowel AH: the jaw eases down into a soft open oval, lips relaxed and neither "
    "spread nor pursed, the edge of the upper front teeth just visible at the top of the "
    "opening and the tongue lying flat and low inside.",
    "THIS IS THE WIDEST SHAPE IN THE WHOLE SET AND IT IS STILL SMALL. The lips part by "
    "roughly the THICKNESS OF HER LOWER LIP and NO MORE - the relaxed 'ah' of ordinary "
    "conversation, not a yawn, not a shout, not an open-wide-for-the-doctor mouth. The "
    "chin must barely drop and the cheeks must stay relaxed."),

 "eh": ("e as in bed",
    "Mid vowel EH: jaw slightly open, lips very slightly spread horizontally, the edge "
    "of the upper front teeth visible, tongue low and relaxed. Not as open as AH, not as "
    "spread as EE.",
    "The lips part by about HALF her lower lip's thickness - roughly half the AH "
    "opening. The spreading is barely perceptible."),

 "ih": ("i as in sit / ee",
    "Close front vowel IH/EE: lips slightly spread horizontally, jaw barely open so the "
    "teeth stay close together, a narrow slit with the upper teeth just showing. The lips "
    "must NOT be rounded.",
    "The lips part by about ONE THIRD of her lower lip's thickness. The spread is SLIGHT "
    "- the mouth is only a fraction wider than neutral. Do NOT stretch it into a grin, do "
    "NOT tense the mouth corners and do NOT expose the gums."),

 "oh": ("o as in go",
    "Rounded vowel OH: lips softly ROUNDED, eased a little forward, the opening slightly "
    "taller than it is wide, the inside of the mouth softly shadowed warm rose rather "
    "than black.",
    "The lips part by about TWO THIRDS of her lower lip's thickness. The rounding is "
    "MODERATE - the mouth stays about 85 percent of its normal width. Do NOT pinch it "
    "into a small tight circle and do NOT drop the jaw."),

 "oo": ("u as in boot / w",
    "Close rounded vowel OO/W: lips gently pursed into a small soft round shape and eased "
    "forward. Smaller and more forward than the OH shape.",
    "The opening is small - about one tenth of her lower lip's thickness. The purse is "
    "GENTLE: the mouth stays about four fifths of its normal width. Do NOT pinch the lips "
    "into a tiny kiss, do NOT hollow the cheeks and do NOT push the lips far forward."),

 "blink": ("eye blink", "__BLINK__", ""),
}

# QA targets: (max aperture / mouth-width ratio, expected width vs neutral).
# Same numbers the prompts describe, so the verifier and the prompt agree.
TARGETS = {
 "closed": (0.03, 1.00), "PP": (0.03, 0.98), "FF": (0.06, 1.00),
 "TH":  (0.09, 1.00), "DD": (0.09, 1.00), "nn": (0.06, 1.00),
 "kk": (0.11, 1.00), "CH": (0.08, 0.90), "SS": (0.06, 1.00),
 "RR":  (0.07, 0.90), "ah": (0.24, 0.97), "eh": (0.14, 1.00),
 "ih":  (0.10, 1.03), "oh": (0.17, 0.85), "oo": (0.06, 0.82),
 "blink": (0.03, 1.00),
}


# Pass-3 calibration: pass 2 undershot these six. Same conversational scale,
# raised just far enough that each shape stays legible next to the closed mouth.
OPENING_OVERRIDE = {
 'FF': (
    "The gap stays small - about one fifth of her lower lip's thickness - but THE "
    "EDGE OF THE UPPER FRONT TEETH MUST BE VISIBLE resting on the lower lip, or the "
    "shape is indistinguishable from a closed mouth. The mouth must NOT be pulled "
    "wide, the upper lip must NOT curl up to expose the gum line, and the teeth must "
    "NOT be bared - no snarl, no sneer, no anger."),
 'SS': (
    "The lips part just enough to show a NARROW HORIZONTAL SLIT with the white edges "
    "of the upper and lower front teeth VISIBLE behind it - about one fifth of her "
    "lower lip's thickness. The slit must be clearly visible; do not close the mouth. "
    "The jaw must NOT drop and NO tongue may show."),
 'kk': (
    "The lips part by about ONE THIRD of her lower lip's thickness into a small "
    "relaxed oval with a clear softly shadowed warm opening behind it. Small, but "
    "unmistakably open. The jaw must NOT drop far."),
 'ah': (
    "This is the WIDEST shape in the set, so it must read as clearly open - while "
    "still being a conversational opening, never a yawn or a shout. The lips part by "
    "about THREE QUARTERS of the thickness of her lower lip, showing the edge of the "
    "upper front teeth and a softly lit warm oral space below. The chin drops only "
    "slightly and the cheeks stay relaxed."),
 'eh': (
    "The lips part by about HALF her lower lip's thickness - clearly less open than "
    "AH, but clearly MORE open than the narrow EE/IH shape. The edge of the upper "
    "teeth shows. The horizontal spread is barely perceptible."),
 'oh': (
    "The lips part by about HALF her lower lip's thickness into a clearly ROUNDED "
    "opening - the rounding must be obvious at a glance. The mouth stays about 85 "
    "percent of its normal width. Do NOT pinch it into a small tight circle and do "
    "NOT drop the jaw."),
}

BLINK_PROMPT = (
    "Edit this portrait photograph. This is a BLINK frame for a talking-head "
    "animation, so the ONLY thing that may change is the eyes.\n\n"
    "Close BOTH EYELIDS FULLY and naturally, as in the middle of a relaxed blink - "
    "smooth closed lids, the lash line resting on the lower lid, a natural soft "
    "eyelid crease. Do not squeeze the eyes shut, do not wrinkle the nose, do not "
    "raise or lower the eyebrows.\n\n"
    "ABSOLUTELY UNCHANGED, pixel for pixel: the mouth and lips, identity and facial "
    "bone structure, head position, head angle, head size, camera framing, crop, "
    "eyebrows, nose, ears, hair, jewellery, clothing, shoulders, skin tone, freckles, "
    "makeup, lighting, colour grade and background. Do not re-frame, re-pose, "
    "re-light or retouch. Keep photographic realism with visible skin texture."
    + CLOSER)

ORDER = ["closed", "PP", "FF", "TH", "DD", "nn", "kk", "CH", "SS", "RR",
         "ah", "eh", "ih", "oh", "oo", "blink"]

EYE_SHAPES = {"blink"}


def pose_clause(yaw, roll):
    if yaw is None:
        return ""
    if abs(yaw) < 6 and abs(roll) < 6:
        return ("The head faces the camera STRAIGHT ON, perfectly frontal and level. "
                "Draw the mouth SYMMETRICALLY about the vertical midline, with both "
                "mouth corners the same distance from the centre of the philtrum.\n\n")
    side = "her left" if yaw > 0 else "her right"
    return (f"IMPORTANT: the head is TURNED about {abs(yaw):.0f} degrees toward {side} "
            f"and tilted about {abs(roll):.0f} degrees. The mouth must be drawn in that "
            f"same perspective - foreshortened toward the far cheek, NOT symmetric and "
            f"NOT straight-to-camera.\n\n")


def prompt_for(name, yaw=None, roll=None):
    if name in EYE_SHAPES:
        return BLINK_PROMPT
    group, desc, opening = SHAPES[name]
    opening = OPENING_OVERRIDE.get(name, opening)
    return (BASE + AMPLITUDE + DENTAL_CONTINUITY + ORAL_RENDERING
            + pose_clause(yaw, roll)
            + f"MOUTH SHAPE TO RENDER - viseme '{name}' ({group}):\n{desc}\n\n"
            + f"HOW FAR THE MOUTH OPENS:\n{opening}" + CLOSER)
