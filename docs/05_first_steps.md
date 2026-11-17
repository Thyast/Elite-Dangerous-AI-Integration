
# First Steps

If you have not already done so, you should first complete the [Getting Started](./index.md) guide. This guide will help you set up your AI and get you started with the basics.

Once you have completed the setup, you can start using the AI in-game. The AI can help you with a variety of tasks, such as navigating the galaxy map, managing your ship's systems, and providing information about the game world. The AI can also assist you in combat, mining, trading, and other activities.

There are several configuration options, that you can adjust to customize the AI to your liking.

## Character Prompt

The AI Character Prompt is a description of the AI's personality. You can modify this text to your liking – if you want the AI to talk about your ships cat, you can mention "We have a cat on our ship that likes to get in the way of my controls when it wants attention.", causing the AI to role-play and mention the cats current whereabouts regularly. More examples can be found on our discord server.

## Audio

The integration uses your microphone for speech recognition. We support voice activation and push-to-talk (PTT). When using voice activation, we recommend using headphones to avoid the AI hearing itself, which causes a feedback loop.

## Event Triggers

Changes in the game world can trigger events that the AI can respond to. For example, when the ship is in danger, the AI can be programmed to inform you. These reactions are configured in the UI in the **Reactions** tab of the settings, where you can choose to silence some events or have the AI respond to others, making the AI more or less chatty.

For a more in-depth guide on reactions and event triggers, see the [Reactions](./25_reactions.md) page.

## Keyboard Bindings

Certain keyboard bindings are required for the AI to trigger game actions. In the **Actions** tab of the settings, the UI will inform you if actions are unassigned, conflicting, or unsupported. Actions can be toggled on and off to disable unwanted ones, such as ejecting all cargo.

For a full list of actions and their required keyboard buttons, see the [Actions](./20_actions.md) page.

## Avatars

Avatars allow you to give the AI a face. You provide a 2×2 grid with equal sections; each section shows the same styled character in one of four attitudes:

| <img src="../ui/src/assets/cn_avatar_default.png" alt="Default avatar grid" width="120" /> | **Top-left:** the character is listening<br>**Top-right:** the character is speaking<br>**Bottom-left:** the character is thinking<br>**Bottom-right:** the character is performing searches or actions |
|----------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## VR Overlay Placement

### Showing the full interface in VR

Under **Advanced Settings > Overlay > Overlay Mode**, choose **VR Overlay / Full Window**, then restart the assistant. This displays the application GUI in the headset, including plugin tabs such as ship upgrade plans, instead of only the transparent avatar/chat overlay. The interface remains visible when the assistant is idle. Selecting a runtime tab or a plugin subtab on the PC also selects it in VR; voice navigation and scrolling commands continue to work.

The VR interface uses the existing assistant process; it does not start another assistant. Start/stop controls remain in the PC window. **VR Overlay / Reduced Window** preserves the release overlay's lightweight avatar/chat/HUD behavior, including hiding chat/avatar content at rest. Its placement preview and settings remain available. The full-window mode is world locked; its offline settings only configure VR/OpenXR rendering and tracking. The former VR Content selector is replaced by these two modes; existing full-interface configurations migrate automatically.

Full-window position, rotation, size, curvature, reduction and reduced opacity are adjusted **in game** and saved per ship size **S/M/L**, using the current ship's `LandingPadSize`. Ships whose size is unknown use a profile keyed by ship type instead. Changing ships loads the matching profile without recentering. If none exists, the last used full-window settings are retained; a new profile is saved when you make and save an adjustment. These profiles are independent of HUD settings and survive assistant/game restarts. A ship change during a position adjustment waits for confirmation or cancellation before loading the new profile.

With **UI actions** enabled, use voice commands such as "show navigation", "show materials", or "show the ship upgrade plan". The assistant can select built-in tabs and registered plugin tabs/subtabs using `showUI`. "Scroll down", "scroll up", "go to the top", and "go to the bottom" scroll the active panel using `scrollUI`. The same commands work in your conversation language, for example "affiche le plan d'amelioration" or "descends dans la page". Voice activation and a provider supporting tool calls must be configured; no VR controller input is required for this navigation.

Plugin subtab names are available in the registered languages, not only as internal keys. With the Ship Upgrade Manager running, say **"affiche la progression"**, **"affiche les plans"**, **"affiche les plans a debloquer"**, or **"affiche la route des ameliorations"**. English examples are **"show progress"**, **"show plans"**, **"show unlockable blueprints"**, and **"show route in ship upgrades"**. These select the corresponding subtab on the PC and in VR without calculating a route or modifying the plan. With the action cache enabled, these registered French/English commands are resolved directly. Ambiguous names shared by several panels require the panel name, for example **"affiche la progression du module ameliorations"**. Bare **"show route"** keeps opening the built-in navigation route.

Plugins can also associate business names with their tabs/subtabs independently of the displayed label. For example **"affiche la progression de l'amelioration du vaisseau"** or **"affiche le suivi des ameliorations"** opens the upgrade progression subtab even when the GUI is in English. The assistant receives the labels in every registered language and the plugin's explicit voice aliases.

To expand or collapse content in the active tab/subtab, say **"deploie la deuxieme section"**, **"replie la section 2"**, or use its name, for example **"deploie les compartiments essentiels"**. English examples are **"expand the second section"** and **"collapse Core Internal"**. Section names can be translated even when the GUI uses another language. This controls collapsible grids, list categories and expandable details; it never starts a plan or changes plugin data. Indices start at **1**, in display order, and count only headers in the active subtab that are not hidden by a collapsed parent, not headers in other tabs. Confirmation waits for the UI's actual expanded state. Missing/ambiguous names, invalid indices and an unresponsive UI are reported explicitly rather than announced as success.

Say **"hide the window" / "hide the GUI"** (French: **"cache la fenetre" / "cache la GUI"**) to temporarily hide the full VR interface using `setUIVisibility`. Only the original transient chat remains: it appears during the conversation and disappears when the assistant is idle, without an avatar or generated HUD, even if Show Chat is disabled. Say **"show the window" / "show the GUI"** (**"montre la fenetre" / "montre la GUI"**, or **"affiche la fenetre" / "affiche la GUI"**) to restore the same GUI, tab, subtab and content. French show/hide commands also accept the optional suffix **"de COVAS:NEXT"** or **"COVAS:NEXT"**, for example **"affiche la fenetre de COVAS:NEXT"** or **"cache la GUI COVAS:NEXT"**. Selecting another tab does not cancel the hidden state. These commands do not hide the PC window, restart the overlay/assistant, or change the saved VR content setting; they only affect the full-interface VR mode.

### Adjusting placement

Use the HUD mode's **Size** and **Distance** settings, or the full window's voice adjustments below, to make the interface easier to read in the headset.

For **adaptive window size**, use a world-locked overlay and set the **Area reduction factor** to a value greater than 1. In the full-window mode, do this by voice in game, not in the settings. Factor **1** is the default and turns adaptive sizing off entirely: no zoom sampling or opacity reduction runs. There is no separate enable checkbox. Values below 1 are rejected. The configured size is the full, 100% reading size. Looking away divides the **surface area** by this factor, so width and height are each divided by its square root: factor 5 means about 44.7% of the full width and height, not 20%. While reduced, the window also fades to the **Reduced window opacity** (10–100%, default **25%**), then returns to 100% opacity when you look at it again, while tracking is lost, and during voice placement adjustment. Existing enabled zoom configurations preserve their area factor (or use 5 if it was absent); previously disabled zoom migrates to factor 1. Restart the assistant after changing HUD settings in the UI. Full-window voice changes apply live, including switching from factor 1 to 5 and back. The same behavior works with **OpenVR on Windows/Linux x64** and **OpenXR application-integration mode on Windows x64**; the active overlay backend automatically selects the pose reader. Separate OpenXR overlay/test sessions are not currently supported by gaze tracking.

For **[EDVR](https://github.com/characterecho-sean/edvr-unofficial-patch) with Virtual Desktop/VDXR**, keep your existing OpenXR runtime. The OpenXR tracking setup is available in both VR modes, independently of the reduction factor. Choose **Install / update OpenXR gaze integration** and confirm. A game-restart notice appears only when installation, removal or a version/registration change affects an already connected OpenXR host; repeating an unchanged installation does not request a restart. The notice clears when that host closes or is replaced. Restart notices for HUD settings compare them with the actual live overlay; full-window adjustments are applied in game.

This installs a small per-user OpenXR API layer in addition to the overlay rendering integration. It reads the host session's headset pose in STAGE space (LOCAL when STAGE is unavailable), matching the overlay's world anchor. It does not modify EDVR, replace the runtime, or require SteamVR. OpenVR needs no additional tracking layer, so these installation controls are hidden for an OpenVR backend. OpenComposite does not implement the **OpenVR overlay API** required by this application; it is not a supported OpenVR overlay backend. This is not a blanket restriction on all OpenXR API-layer hosts: the Windows rendering layer accepts compatible D3D11/D3D12 hosts. EDVR is the native OpenXR route documented here for Elite Dangerous.

At overlay startup, **World Locked** uses the first valid headset height as its
vertical reference, plus the saved vertical offset, rather than assuming an eye
height of 1.4 m. It then stays fixed; it does not follow later head movements.
This works independently of adaptive sizing and waits if the VR application
starts later. Only C:N's height is initialized: its world heading, horizontal
position and saved settings are unchanged, and no recenter command is sent to
Elite or OpenKneeboard. Head Locked is unchanged. An explicit recenter takes
precedence over a still-pending initialization. On OpenXR this needs the same
tracking integration described above, even with adaptive sizing off. Waiting
or failed initialization is shown in Placement; a tracking or placement error
keeps the existing placement and is logged rather than claiming success.

The enabled status checks the OpenXR manifest format as well as the registry flag. Older development builds could register a manifest rejected by the loader because its implementation version was a number instead of a string. **Install / update** repairs that manifest without replacing the tracking DLL; restart an already-running game afterward.

Adaptive sizing uses the headset's forward direction, not eye tracking. Looking at the reduced window for 0.2 seconds smoothly restores 100% size; looking away for 0.4 seconds reduces it. Hysteresis lets you read across the full panel without immediately reducing it. Position, rotation and curvature are taken into account. Tracking is sampled at 20 Hz with no queued animation commands.

Adaptive sizing is off by default and requires a world-locked window. OpenXR tracking data may appear after overlay creation or the game's first VR frame: the overlay waits at full size and reconnects automatically, checking once per second, without requiring another assistant restart. The settings show this waiting state until tracking connects; it does not mean that installation failed. If no data arrives after the game has entered VR, check the gaze integration registration and whether the game was restarted after installation. Unsupported tracking and access/protocol errors still produce an explicit warning. Temporary tracking loss restores full size and is reported in the logs; recovery re-enables adaptive sizing. Headset poses are held only in local, process-scoped shared memory, with no network transmission or pose logging. The OpenXR layer registration can be removed separately from the overlay integration; restart VR applications after installing, updating or removing it.

### Voice adjustments during a VR session

To request a **coordinated VR recenter**, say **"reinitialise la vue"** or **"recentre la vue"** (English: **"reset the view"** or **"recenter the VR view"**). Enable **Game Actions > recenterVRView** and assign a keyboard or mouse binding to Elite's **Reset HMD Orientation** (`HMDReset`) control. OpenKneeboard is optional: when running with its API available, it is also recentered. Keep your head still, looking forward towards your HOTAS, until the completion message.

COVAS first reanchors its world-locked overlay at the current headset position and horizontal heading, preserving configured offsets, distance, pitch and yaw. It stays horizon-level instead of inheriting your head's pitch or roll. Subsequent position adjustments and cancellation use this new session origin; no saved placement, size, visibility or adaptive-size setting is reset. A head-locked overlay already follows the headset and requires no repositioning. The world-locked path uses the existing headset tracking readers even if adaptive sizing is disabled; **OpenXR requires the gaze integration** described above and a fresh tracking pose.

After native acknowledgement, COVAS restores a minimized Elite window, verifies foreground focus, sends its configured `HMDReset` binding (not a hard-coded F12), and, when available, sends `RECENTER_VR` through OpenKneeboard's [installed C API](https://openkneeboard.com/api/). OpenKneeboard does not provide an acknowledgement API: the completion message confirms COVAS positioning and **dispatch** of commands, not verified visual success. If OpenKneeboard is not installed, not running, or its API file is missing, it is explicitly skipped without turning the game/COVAS recenter into an error or claiming a command was sent to it. Missing tracking, game bindings or game focus, and failures loading or calling an available OpenKneeboard API remain explicit errors; partial completion names the affected components. No keyboard input is sent if Windows denies game activation.

This coordinates **these three applications on Windows**, not the Quest's global system origin or unrelated applications. It is not an emulation of the long press on the Meta button. The supported explicit commands run locally after voice activation, without an LLM call, even with the lightweight HUD or during a placement adjustment; they do not validate or cancel that adjustment. Recentring is not exposed as an AI tool: game startup, loading a save, game-mode changes and tracking reconnection cannot trigger it automatically. Use one of the explicit commands above when your head is in the intended position. **"Focus Elite"** is a window-focus command, not a recenter command, and remains available to external tools such as VoiceAttack.

With **UI actions** enabled and the full VR GUI running, say **"adjust the position of the window"** or **"ajuste la position de la fenetre de Covas:Next"**. The assistant explains the controls, temporarily reveals a hidden GUI, and suspends adaptive sizing at 100% size. These French/English adjustment commands are handled locally after speech recognition, without an LLM/tool call per movement:

Shorter requests such as **"regle la fenetre"**, **"on va regler la fenetre d'overlay"** and **"adjust the overlay"** also start adjustment mode. The local command parser accepts the STT variants **Kovac / Kovacs / Covac / Covacs** for COVAS and **overlaid** for overlay. It removes the configured assistant name if it remains in the accepted command, including **Simon / Simonne** when the name is Simone; the same applies to movement, confirmation and factor commands. These aliases do not rewrite the displayed transcript or relax voice-activation gating. Normal requests to show/hide the GUI or change tabs still use their existing actions.

- **"plus haut" / "plus bas"**, **"a gauche" / "a droite"**, **"plus pres" / "plus loin"** (English: **"up" / "down" / "left" / "right" / "closer" / "farther"**).
- Optionally specify a distance, for example **"plus haut de 10 centimetres"**, **"a droite de 0,2 metre"**, or **"up by 5 cm"**. Without a distance, the step is **5 cm**.
- **"rotation sur l'axe horizontal de 5 degres"** changes pitch; **"rotation sur l'axe vertical de moins 10 degres"** changes yaw. English commands are **"pitch by 5 degrees"** and **"yaw by minus 10 degrees"**. Without a value, the step is **5 degrees**; negative values rotate the other way.
- Spoken signs (**"plus" / "moins"**, **"positive" / "minus"**) and symbolic **`+` / `-`** (also `−`) are equivalent, with or without a space before the number. For example, **"rotation sur l'axe vertical de - 5 degres"** and **"plus haut de -5 centimetres"** are accepted. A negative movement reverses the requested direction; the usual placement limits still apply.
- **"taille de la fenetre a un metre" / "set window size to 1 metre"** sets the full window size (0.1–3 m).
- **"courbure a trente degres" / "set curvature to 30 degrees"** sets curvature (0–180 degrees; 0 is flat).
- **"valider la position" / "confirm position"** saves the position to the current ship profile without restarting the overlay, game or assistant.
- **"annuler" / "cancel"** restores the exact position from the start of the adjustment without saving the trials.

Changes appear immediately in the headset. The full-window mode has no offline placement preview or sliders; the HUD mode retains them. During this mode, all other utterances receive adjustment guidance rather than being sent to game actions. The distance offset supports **-1.5 to +1.5 metres**, both here and in the HUD settings sliders. The other placement limits still apply; an out-of-range or rejected update is reported rather than silently clamped. A save failure leaves adjustment mode open to retry confirmation or cancel. Finishing restores the previous GUI visibility and resumes adaptive sizing at the new (or restored) position.

The area factor can also be changed live, independently of the position transaction: **"regle le facteur de reduction a cinq"**, **"facteur de reduction de la fenetre 9"**, or **"set window reduction factor to 9"**. **"Regle le facteur de zoom a cinq"** and **"set zoom factor to five"** are aliases for the same area reduction, not enlargement. A factor above 1 enables adaptive sizing; **"facteur de reduction a un"** disables it and restores full size and opacity immediately. Tracking failures are explicit. Size, curvature, factor and opacity save immediately to the current ship profile, including during position adjustment; cancelling a position adjustment does not undo these separately saved settings. The reduced-window opacity can be changed the same way: **"opacite a 40 %"**, **"regle l'opacite de la fenetre reduite a 25 pour cent"**, **"transparence a 75 %"** (75% transparency = 25% opacity), or **"set reduced opacity to 30 percent"**. It is saved immediately and accepts 10–100% opacity (0–90% transparency). STT still uses your configured voice provider; only command interpretation is local.

During in-game adjustment, a blue outer border marks the full GUI. With adaptive sizing enabled on a world-locked window, a centered, dashed blue inner frame shows its reduced dimensions and updates after each acknowledged factor change. The GUI itself stays at 100% while adjusting. Both dimensions are divided by the square root of the factor: factor 5 gives about 44.7% width and height, or 20% area. The settings placement preview shows the same reduced-size outline, including the configured position, rotation and curvature. The frames disappear in game when adjustment ends; no inner frame is shown when adaptive sizing is disabled or the anchor is Head Locked.

Developers building Windows packages must first restore the pinned native compiler with `python -m pip install --target build\openxr-gaze-tools -r native\openxr-gaze\requirements.txt`, then run `npm run build:vr-gaze`. The build downloads headers from a pinned OpenXR SDK revision and includes the tracking DLL in Windows packages. `npm run test:vr-openxr` tests the compiled layer against a simulated host and verifies actual cross-process pose transfer; it does not replace testing EDVR/VDXR in a physical headset.

In **Advanced Settings > Overlay**, select **VR Overlay** to adjust the window's placement. The live preview shows an empty orange forward-view frame with horizontal and vertical center axes, and the overlay window in blue. It updates as you adjust the horizontal and vertical offsets, distance, pitch, yaw, size, and curvature, without requiring a connected VR headset.

The controls and preview update locally while you drag. Configuration saves are grouped after 200 ms without movement, or immediately when you release the slider, so saving does not queue up old slider positions.

The preview uses an illustrative projection, not your headset's exact field of view. The blue window's width, height, and displacement from the center axes are shown at half scale to keep adjustments within the reference frame more easily; this does not change the actual VR size or placement. Parts of the blue window outside the orange frame are outside this reference view. For **World Locked**, the reference viewer faces forward at an eye height of 1.4 m; moving your headset changes the actual view.

## Plugins

Explore [Plugins](./plugins/index.md) to enhance your experience: fast, on-device speech-to-text (STT), text-to-speech (TTS), and embedding.

## Upgrading the AI

You will automatically receive a notification when a new version of the AI is available during the launch of the application. After downloading the new version, run the MSI installer. The one-click installer updates your existing installation; your configuration and conversations are migrated automatically.
