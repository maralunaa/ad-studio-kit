# Shot recipe cards (Video Studio V2)

One YAML file per shot type, `<key>.v<N>.yaml`. To change a card, copy it to the next version and edit
the copy; the newest version is used and every shot records the version that made it.

Card format adapted from video-shotcraft's shot cards (Apache-2.0; see THIRD_PARTY.md): a one-line
summary, where the shot is used, length, energy, intent, the motion, a parameter table with tuning
notes, and known pitfalls. The still prompt follows banana-claude's "locks and freedom" structure
(MIT; see THIRD_PARTY.md). Fields:

    name, label, summary, use, duration_s, energy (low | medium | high)
    product_types  which product types the card is for (fruit | candle | other); every new product of that
                 type gets the card. Prompts use the product's own setup, so nothing is product-specific.
    route        kling | veo | seedance   (which video model; fallbacks in config VIDEO_FALLBACKS)
    audio        none | ambient           (ambient = the model's own sound, Veo/Seedance)
    from_images  approved image types that can be the start still (else a still is generated)
    end_from_images  approved image types the clip should end on (e.g. a real cut photo); optional
    strict_qa    true for multi-fruit or busy scenes: consistency must be 5, warping at least 4
    count_check  false for wide shots where fruit can't be counted (default true)
    styles       style keys the card is made for only (e.g. supermarket_fruit for Us vs supermarket); other styles'
                 scripts never use it
    cast         true when the reserved style's scripts may put the card on a line (claymation, ripening); without it the
                 style uses the card itself (the supermarket split)
    stop_motion_fps  play the shot at this frame rate (held frames) for a stop-motion feel
    facts_category  approved facts of this category go into the still prompt (e.g. ripeness for the time-lapse)
    qa_skip      rubric criteria not scored for this card (e.g. fruit_accuracy for the deliberately pale supermarket
                 fruit); qa_note tells QA why
    still        locks / freedom / composition / lighting  -> the start still prompt
    motion       camera / change                            -> the clip prompt (one small A->B change)
    negative     what must not appear (sent where the model takes a negative prompt)
    params       [{name, value, feel}]   tuning notes
    pitfalls     known ways the shot goes wrong
