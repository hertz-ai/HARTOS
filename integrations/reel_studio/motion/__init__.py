"""integrations.reel_studio.motion -- Reel Studio's animated overlays (amendment A14).

Four templates of our own (project/templates.js, HARTOS source, Apache-2.0,
no third-party template code): a kinetic title, a logo sting, a
booking/price end card and a before->after wipe.  Remotion
(remotion.dev) renders each as a clip with alpha; pass 2 of the reel render
lays them over the cut through core.media_bin (media/render.py), inside
contracts.SAFE_ZONE and never over her face in the final look's hold
(contracts' 'overlays' rule), and QA masks them (media/qa.py).

Nothing of Remotion is packaged (owner ruling A16): its npm packages and
its headless Chrome are set up at runtime, on demand, into the models dir by
RuntimeToolManager (the 'reel_motion' row: model_storage.install_npm_project
runs npm install and project/prepare.js), after the owner's yes on the one
consent card and a free-disk check, while a reel waits a bounded time.

OFF BY DEFAULT behind ONE flag, the row's ``enabled_by``
(HEVOLVE_REEL_MOTION), until the owner confirms which Remotion license
applies (NOTICE records its terms).  Off, not allowed, still setting up or
failed, a reel gets the plain-ffmpeg versions: the title is the hook text,
the logo is the still logo, the end card is a boxed text, the wipe is the cut.

Her agent asks for overlays with the on-demand reel_motion tool (tools.py),
which keeps her choice in her voice profile (motion_overlays); every
reel_make then applies it.  Deterministic: no model chooses or places them.

Modules: layout (pure: where and when each overlay plays), runtime (is
Remotion ready, set it up, render), compose (the pipeline's step).
"""
