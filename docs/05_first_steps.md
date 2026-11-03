
# First Steps

If you have not already done so, you should first complete the [Getting Started](./index.md) guide. This guide will help you set up your AI and get you started with the basics.

Once you have completed the setup, you can start using the AI in-game. The AI can help you with a variety of tasks, such as navigating the galaxy map, managing your ship's systems, and providing information about the game world. The AI can also assist you in combat, mining, trading, and other activities.

There are several configuration options, that you can adjust to customize the AI to your liking.

## Character Prompt

The AI Character Prompt is a description of the AI's personality. You can modify this text to your liking – if you want the AI to talk about your ships cat, you can mention "We have a cat on our ship that likes to get in the way of my controls when it wants attention.", causing the AI to role-play and mention the cats current whereabouts regularly. More examples can be found on our discord server.

## Audio

The integration uses your microphone for speech recognition. We support voice activation and push-to-talk (PTT). When using voice activation, we recommend using headphones to avoid the AI hearing itself, which causes a feedback loop.

### Name-triggered dialogue

Choose **Name-triggered Dialogue** under **Microphone Mode** in the general or advanced audio settings. The existing microphone modes, including Push-to-Talk, are unchanged. Start or restart the assistant after changing the microphone mode or activation settings.

In Name-triggered Dialogue, microphone activity alone does not interrupt a spoken
reply or game announcement: interruption waits for a recognized, accepted command
or dialogue control. Ambient sound or rejected standby speech therefore cannot
silence an announcement. Other microphone modes retain immediate interruption
when recording starts.

The **active avatar's first name** is used for activation. It is separate from the technical **Profile name** displayed in the character selector. Each avatar has a **First name** field in Character Settings, initialized to `Simone`, including existing profiles when upgrading. A profile named `Default (Copy)` therefore responds to "Simone", not "Default Copy". Edit or duplicate an avatar to change its first name, then select it. Duplicating a profile preserves its first name without adding "(Copy)" to the spoken name. Set the global **inactivity timeout** (initially 10 seconds) in the microphone settings. Restart the assistant after changing the avatar or its first name. Say the first name at the beginning of a phrase:

- "Simone, plot a route to Sol."
- "Simone?" on its own, then wait for an acknowledgment before giving a command.
- Optionally precede the name with a configured prefix: by default, "Hey, Simone", "OK, Simone", "Okay, Simone" or "Dis, Simone", with or without a command in the same phrase.

Activation is not restricted to French. Use your configured name on its own or with a configured prefix; recognition depends on your STT provider and its language settings. Commands can be spoken in a language supported by your configured STT.

Once the dialogue is active, give follow-up commands without repeating the name. The timeout starts after the assistant finishes responding, including voice playback, and renews after each exchange. It does not cut off speech or processing already in progress. An inactivity timeout returns to standby with a quiet, short descending two-tone beep, generated locally without an LLM or speech-synthesis request. It uses the configured audio output and output volume, without character voice effects, and also works when TTS is disabled. No spoken acknowledgment or conversation event is generated for a timeout.

To keep the current session open without an inactivity timeout, say "keep listening" or "reste en écoute" as a complete utterance. You can also open a session this way: "Simone, reste en écoute". The assistant gives a short confirmation without an LLM request, then keeps accepting follow-up commands until an explicit closing phrase. Mentioning the phrase inside another request does not suspend the timeout. The next session uses the normal configured timeout again; this command does not change the saved settings.

To close the session explicitly, say a configured closing phrase as a complete utterance. Default phrases include "that's all", "I'm done" and "I am done" in English, and "ça sera tout" and "j'ai terminé" in French. You can edit or translate these phrases in the external phrase file described below.

Calling the name alone produces a short readiness acknowledgment; explicitly closing the session produces a short confirmation. These direct acknowledgments use your configured TTS voice without an LLM request. English is the default, with French equivalents also provided in the phrase file. With TTS disabled, acknowledgments are shown as text and a warning explains why no voice was played.

A badge in the application and visible overlays shows the active first name and distinguishes **Standby**, **Dialogue active** and recognition errors. Bundled translated UI text, including this badge, follows the browser's language: French for `fr` locales, otherwise English. For example, a French interface displays **Simone: Veille**. This UI language does not change the STT, TTS or character language. Game-event reactions and typed input continue to work outside voice sessions. Closing a session stops admission of new voice commands; it does not cancel game actions already started.

This mode **replaces the required-word filter** rather than adding another filter. The old required-word value is preserved for other microphone modes. Unlike a session, the old filter requires its text in every phrase and checks it only after transcription.

#### Customizing voice phrases

Invocation prefixes, closing phrases, keep-listening phrases and direct acknowledgments are loaded from [src/data/voice_phrases.json](../src/data/voice_phrases.json), not embedded in the Python code. This file is also bundled with the packaged backend, in its `data` directory alongside the quest catalog. Its `default_language` is `en`, and `languages` contains separate `en` and `fr` objects.

| JSON field | Purpose |
|---|---|
| `invocation_prefixes` | Optional words before the configured name. An empty list adds no prefixes for that language. |
| `closing_phrases` | Complete utterances that close the dialogue. |
| `keep_listening_phrases` (optional) | Complete utterances that suspend the current session's inactivity timeout. An empty or omitted list disables this control for that language. |
| `wake_acknowledgments` | Responses to calling the name without a command; one is chosen randomly. |
| `close_acknowledgments` | Confirmations of explicit closing or a keep-listening request; one is chosen randomly. |
| `language_names` (optional) | Names or aliases for an explicit TTS/STT language setting, such as `English` or `French`. Language codes work without aliases. |

Edit the UTF-8 JSON lists under `languages.en` or `languages.fr`, or add another language using its lowercase ISO 639-1 code, then restart the assistant. The four original fields (`invocation_prefixes`, `closing_phrases`, `wake_acknowledgments` and `close_acknowledgments`) are required for each language. Each entry must be a string containing at least one letter or number; `closing_phrases`, `wake_acknowledgments` and `close_acknowledgments` must be non-empty. The default language must have its own entry. Invalid or missing files produce an explicit startup error rather than falling back to hard-coded phrases. Recognition ignores case, accents and punctuation, but closing and keep-listening phrases must still match the entire utterance. Prefix entries do not include the activation name, which comes from the active avatar's first name, never its technical profile name. The timeout, activation STT source, and first-command strategy remain global and are not stored per avatar.

All configured languages' prefixes, closing phrases and keep-listening phrases are recognized, so users can speak more than one language during a dialogue. Acknowledgment language is selected in this order:

1. An explicit TTS language, from the provider's character-level `language` setting or the TTS model's configured language.
2. An explicit language configured in the main STT.
3. Automatic local detection from accepted command text, if a compatible detector plugin is installed. A closing or keep-listening phrase identifies its language directly from the catalog, even without a detector plugin.

Automatic or multilingual language settings do not fix a single language. A name-only call, with or without a prefix, cannot reliably identify the speaker's language: it reuses the last known language, or English before one is known. Ambiguous short commands also preserve the last known language. This memory lasts until the assistant restarts, including across dialogue timeouts and explicit closing.

Automatic detection is optional. CN does not bundle or require Lingua. A separately packaged Lingua plugin can provide local text-language detection through the plugin API; it requires a compatible development build and is not currently an official published plugin. Install a compatible archive manually using the [plugin installation process](./plugins/index.md), then restart CN. There is no automatic plugin installer or runtime dependency download.

Without a detector plugin, name-triggered dialogue still works: explicit TTS/STT languages, catalog-based closing phrases, the last known language and the English default remain available. Startup reports when automatic detection is unavailable. Configured language names must be listed in the catalog's optional `language_names`; language codes such as `en` and `fr-FR` do not require aliases.

A detector plugin receives only accepted voice text, never rejected standby conversation. It does not trigger a second STT call or an LLM request. The Lingua plugin runs offline and includes its models in its own package. Short or mixed-language text may still be ambiguous. If a detected or configured language has no phrase catalog, a warning reports that the default language will be used; add that language's phrases to support its acknowledgments.

An explicit TTS or STT language takes precedence even if a command is spoken in another language. To follow language changes automatically, leave these language settings automatic or multilingual where the provider supports that behavior. The application does not infer a fixed language from a voice name or from the interface language; selecting a multilingual voice alone does not lock acknowledgments to its locale.

The phrases themselves are not machine-translated. Choose acknowledgment wording compatible with your TTS voice. Application updates may replace the bundled file, so keep a copy of your customizations.

#### Transcription choices and privacy

No additional installation is required to use your existing STT provider. You can optionally install the [Parakeet STT plugin](https://github.com/COVAS-Labs/plugin-parakeet-stt), whose multilingual model supports French and English and runs locally on CPU. Install the appropriate release archive through the existing [plugin installation process](./plugins/index.md). The release includes its model; the Windows v0.0.10 archive is approximately 749 MB. The application does not install another model automatically.

| Configuration | Standby transcription | Commands |
|---|---|---|
| Parakeet as the main STT | Local | Local, using the same instance and one transcription per phrase |
| Your existing STT as the activation source | Uses that provider, which may be remote | Uses that same provider |
| Parakeet only as the activation source | Local | Uses your main STT after activation |

In the hybrid configuration, **First command in the invocation** controls a phrase such as "Simone, plot a route to Sol":

- **Retranscribe with main STT** (default): Parakeet recognizes the call, then that same phrase is transcribed by your main STT to preserve its recognition quality.
- **Reuse local Parakeet text**: the first command uses the local text, avoiding a second transcription. Following commands use the main STT.

Calling only the name or a complete named keep-listening/closing phrase in standby never needs the second transcription. These controls use the activation transcript directly, so a second provider cannot misrecognize the name and turn them into an ordinary conversation request. The retranscription setting applies only to conversational requests. **If Parakeet is already the main STT, there is always exactly one model instance and one transcription per phrase**, regardless of a previously saved hybrid preference.

**If activation uses a remote STT, every detected phrase is sent to that provider even in standby.** The name filter prevents unintended commands, not that audio transmission. The settings show this distinction and recommend Parakeet for local standby transcription. Rejected standby speech is not added to application conversation history or content logs; any remote provider's retention policy still applies.

If an optional Parakeet activation selection is no longer available at startup, a visible warning explains the fallback to the configured main STT. Recognition errors during operation close the session and are reported; they do not silently switch to a remote detector. A valid main STT is required. Even with local STT, the configured LLM and TTS may be remote.

Detection happens after a short speech pause, not instantly in the middle of a word. Pronunciation, microphone quality and background noise can affect recognition; use headphones, check the badge and test your chosen name before relying on it in game.

### Google AI Studio text-to-speech

To audition a voice, open **Characters**, select an editable character (duplicate **Default** first if needed), click **Edit**, then expand **Voice & processing**. The **Play voice preview** button speaks the displayed test message using the selected TTS provider, current character voice/instructions/speed, manual audio effects, output device and output level. The message is editable and stays unchanged between clicks: each click performs a new synthesis, so repeated tests can reveal voice or intonation variations. The button waits for playback to finish before allowing another test, and errors appear beside it.

Previews work before starting the assistant, without enabling the microphone, invoking the LLM, or adding a conversation entry. Automatic effects from in-game damage, helmets or SRVs are excluded. For remote TTS providers, the test text is sent to that provider and normal API charges/quotas apply. No generated audio is cached between tests. Changing the test message does not change the character prompt or persist the message in configuration.

Select **Google AI Studio** as the TTS / Speaker provider and enter your Google AI Studio API key. The default speech model is **`gemini-3.8-flash-lite-tts`**. You can set a separate TTS API key or change the model in the provider settings. This integration uses the native Gemini Interactions API, not Google's OpenAI-compatible endpoint.

In the character settings, choose a voice (default: **Kore**) and optionally enter **Voice Instructions**, for example, "Speak French with a calm, warm tone and a measured pace." These directions are sent as structured speech metadata, separately from the words to speak. Use these instructions to control pacing; the legacy numeric speech-speed setting is not sent to this API.

Audio streams as raw 16-bit little-endian PCM at 24 kHz, mono, matching the application playback and audio effects pipeline. The integration requests `audio/l16` explicitly to avoid playing a WAV header as audio. Requests use `store: false` rather than saving Interactions history. Selecting a Google API key does not automatically replace your TTS provider; Google TTS is an explicit opt-in.

According to [Google's model documentation](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash-lite-tts), Flash-Lite TTS prioritizes latency, throughput and cost, supports French, and shares its API schema with **`gemini-3.8-flash-tts`**. The latter prioritizes voice fidelity, expressive acting and difficult pronunciations, and can be selected in the same model field. These are the vendor's positioning claims, not an in-app listening benchmark.

Model access, quotas and billing depend on your Google project; an existing STT key does not guarantee access to this TTS model. HTTP errors (including 403/429), stream errors and malformed audio are reported rather than silently falling back to another provider. For a subjective quality check, compare the same French phrases and voice in [Google AI Studio's speech playground](https://aistudio.google.com/generate-speech), including names, numbers and acronyms used in-game.

## Long-term Memory

Long-term memory uses the **Embeddings** provider, independently of the main LLM.
Changing only the LLM provider does not change this selection. If embeddings
still use OpenAI, they require a valid OpenAI API key even when the LLM uses
Google AI Studio. To use Google for both, select **Google AI Studio** for
Embeddings; its shared Google API key is used unless an embedding-specific key
override is configured. Memory text is sent to the selected embedding provider.

An embeddings authentication error prevents new long-term memories from being
stored. Before changing embedding models, back up existing memories: vectors
from different models are not interchangeable.

## Event Triggers

Changes in the game world can trigger events that the AI can respond to. For example, when the ship is in danger, the AI can be programmed to inform you. These reactions are configured in the UI in the **Reactions** tab of the settings, where you can choose to silence some events or have the AI respond to others, making the AI more or less chatty.

For a more in-depth guide on reactions and event triggers, see the [Reactions](./25_reactions.md) page.

## Keyboard Bindings

Certain keyboard bindings are required for the AI to trigger game actions. In the **Actions** tab of the settings, the UI will inform you if actions are unassigned, conflicting, or unsupported. Actions can be toggled on and off to disable unwanted ones, such as ejecting all cargo.

Localized key names from Elite Dangerous, such as `Key_è`, `Key_ç`, `Key_à`, and `Key_Underline`, are supported, as are named symbols such as `Key_Ampersand`, `Key_Circumflex`, `Key_Dollar`, `Key_ExclamationPoint`, and `Key_SuperscriptTwo`. On Windows, these keys are resolved using the `KeyboardLayout` declared in the game's `.binds` file. This covers the French AZERTY number row and symbols, including shifted symbols such as `¨`, `£`, `µ`, `%`, `§`, `/`, `+`, and `°`. Explicit modifiers are preserved: for example, `Key_è` with `Key_LeftShift` presses the French AZERTY `è/7` key with Shift; there is no need to change the binding to `Key_7`. Dead keys such as `^` and `¨` are sent as physical key presses rather than composed text.

Only the modifiers explicitly recorded in the `.binds` entry are sent, in their saved order. Left and right Shift, Control, and Alt remain distinct; `Key_RightAlt` is never replaced with Ctrl+Alt. Windows is used to find the physical key's scan code, not to generate a text-entry combination.

If Windows reports that a key name requires Shift, Control, or Alt to produce its character, the **Actions** settings show a keyboard mapping warning. The physical key remains available with only its explicit game modifiers. This can indicate an unexpected key name or a layout mismatch. When reporting it, include the action, key name, game layout, and modifier details shown in the warning. Keys that cannot resolve at all appear separately as unsupported bindings.

For a full list of actions and their required keyboard buttons, see the [Actions](./20_actions.md) page.

## Avatars

Avatars allow you to give the AI a face. You provide a 2×2 grid with equal sections; each section shows the same styled character in one of four attitudes:

| <img src="../ui/src/assets/cn_avatar_default.png" alt="Default avatar grid" width="120" /> | **Top-left:** the character is listening<br>**Top-right:** the character is speaking<br>**Bottom-left:** the character is thinking<br>**Bottom-right:** the character is performing searches or actions |
|----------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## VR Overlay Placement

### Showing the full interface in VR

Under **Advanced Settings > Overlay > VR Content**, choose **Full COVAS:NEXT Interface**, then restart the assistant. This displays the application GUI in the headset, including plugin tabs such as ship upgrade plans, instead of only the transparent avatar/chat overlay. The interface remains visible when the assistant is idle. Selecting a runtime tab or a plugin subtab on the PC also selects it in VR; voice navigation and scrolling commands continue to work.

The VR interface uses the existing assistant process; it does not start another assistant. Start/stop controls remain in the PC window. **Avatar, Chat and HUD** preserves the lightweight overlay's existing behavior, including hiding chat/avatar content at rest. The Show Avatar, Show Chat and HUD switches do not hide the full interface.

With **UI actions** enabled, use voice commands such as "show navigation", "show materials", or "show the ship upgrade plan". The assistant can select built-in tabs and registered plugin tabs/subtabs using `showUI`. "Scroll down", "scroll up", "go to the top", and "go to the bottom" scroll the active panel using `scrollUI`. The same commands work in your conversation language, for example "affiche le plan d'amelioration" or "descends dans la page". Voice activation and a provider supporting tool calls must be configured; no VR controller input is required for this navigation.

Plugin subtab names are available in the registered languages, not only as internal keys. With the Ship Upgrade Manager running, say **"affiche la progression"**, **"affiche les plans"**, **"affiche les plans a debloquer"**, or **"affiche la route des ameliorations"**. English examples are **"show progress"**, **"show plans"**, **"show unlockable blueprints"**, and **"show route in ship upgrades"**. These select the corresponding subtab on the PC and in VR without calculating a route or modifying the plan. With the action cache enabled, these registered French/English commands are resolved directly. Ambiguous names shared by several panels require the panel name, for example **"affiche la progression du module ameliorations"**. Bare **"show route"** keeps opening the built-in navigation route.

Plugins can also associate business names with their tabs/subtabs independently of the displayed label. For example **"affiche la progression de l'amelioration du vaisseau"** or **"affiche le suivi des ameliorations"** opens the upgrade progression subtab even when the GUI is in English. The assistant receives the labels in every registered language and the plugin's explicit voice aliases.

To expand or collapse content in the active tab/subtab, say **"deploie la deuxieme section"**, **"replie la section 2"**, or use its name, for example **"deploie les compartiments essentiels"**. English examples are **"expand the second section"** and **"collapse Core Internal"**. Section names can be translated even when the GUI uses another language. This controls collapsible grids, list categories and expandable details; it never starts a plan or changes plugin data. Indices start at **1**, in display order, and count only headers in the active subtab that are not hidden by a collapsed parent, not headers in other tabs. Confirmation waits for the UI's actual expanded state. Missing/ambiguous names, invalid indices and an unresponsive UI are reported explicitly rather than announced as success.

Say **"hide the window" / "hide the GUI"** (French: **"cache la fenetre" / "cache la GUI"**) to temporarily hide the full VR interface using `setUIVisibility`. Only the original transient chat remains: it appears during the conversation and disappears when the assistant is idle, without an avatar or generated HUD, even if Show Chat is disabled. Say **"show the window" / "show the GUI"** (**"montre la fenetre" / "montre la GUI"**, or **"affiche la fenetre" / "affiche la GUI"**) to restore the same GUI, tab, subtab and content. French show/hide commands also accept the optional suffix **"de COVAS:NEXT"** or **"COVAS:NEXT"**, for example **"affiche la fenetre de COVAS:NEXT"** or **"cache la GUI COVAS:NEXT"**. Selecting another tab does not cancel the hidden state. These commands do not hide the PC window, restart the overlay/assistant, or change the saved VR content setting; they only affect the full-interface VR mode.

### Adjusting placement

Use **Size** and **Distance** to make the interface easier to read in the headset.

For **adaptive window size**, select **World Locked**, enable **Reduce the overlay when looking away**, and set the **Area reduction factor** (at least 1, default **5**). The configured size is the full, 100% reading size. Looking away divides the **surface area** by this factor, so width and height are each divided by its square root: factor 5 means about 44.7% of the full width and height, not 20%. Factor 1 disables reduction. This replaces the previous gaze-enlargement behavior; existing zoom settings keep their enabled state and use the new default factor 5, not the old enlargement multiplier. Restart the assistant after changing these settings in the UI. The same behavior works with **OpenVR on Windows/Linux x64** and **OpenXR application-integration mode on Windows x64**; the active overlay backend automatically selects the pose reader. Separate OpenXR overlay/test sessions are not currently supported by gaze tracking.

For **[EDVR](https://github.com/characterecho-sean/edvr-unofficial-patch) with Virtual Desktop/VDXR**, keep your existing OpenXR runtime. The adaptive-size section appears below Placement, Orientation and Appearance; its controls are shown only with **World Locked**. In its OpenXR setup, choose **Install / update OpenXR gaze integration** and confirm. A game-restart notice appears only when installation, removal or a version/registration change affects an already connected OpenXR host; repeating an unchanged installation does not request a restart. The notice clears when that host closes or is replaced. Restart notices for UI settings compare them with the actual live overlay, so already-applied voice changes do not request a restart.

This installs a small per-user OpenXR API layer in addition to the overlay rendering integration. It reads the host session's headset pose in STAGE space (LOCAL when STAGE is unavailable), matching the overlay's world anchor. It does not modify EDVR, replace the runtime, or require SteamVR. OpenVR needs no additional tracking layer, so these installation controls are hidden for an OpenVR backend. OpenComposite does not implement the **OpenVR overlay API** required by this application; it is not a supported OpenVR overlay backend. This is not a blanket restriction on all OpenXR API-layer hosts: the Windows rendering layer accepts compatible D3D11/D3D12 hosts. EDVR is the native OpenXR route documented here for Elite Dangerous.

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
- **"valider la position" / "confirm position"** saves the position to configuration without restarting the overlay, game or assistant.
- **"annuler" / "cancel"** restores the exact position from the start of the adjustment without saving the trials.

Changes appear immediately in the headset and in the PC settings/placement preview. During this mode, all other utterances receive adjustment guidance rather than being sent to game actions. The distance offset supports **-1.5 to +1.5 metres**, both here and in the settings sliders. The other placement limits still apply; an out-of-range or rejected update is reported rather than silently clamped. A save failure leaves adjustment mode open to retry confirmation or cancel. Finishing restores the previous GUI visibility and resumes adaptive sizing at the new (or restored) position.

The area factor can also be changed live, independently of the position transaction: **"regle le facteur de reduction a cinq"**, **"facteur de reduction de la fenetre 9"**, or **"set window reduction factor to 9"**. **"Regle le facteur de zoom a cinq"** and **"set zoom factor to five"** are aliases for the same area reduction, not enlargement. This saves the factor immediately, including during position adjustment; cancelling a position adjustment does not undo a separately saved factor. It does not silently enable adaptive sizing when the option is off or tracking is unavailable. Factor 1 keeps the window at full size. STT still uses your configured voice provider; only command interpretation is local.

During in-game adjustment, a blue outer border marks the full GUI. With adaptive sizing enabled on a world-locked window, a centered, dashed blue inner frame shows its reduced dimensions and updates after each acknowledged factor change. The GUI itself stays at 100% while adjusting. Both dimensions are divided by the square root of the factor: factor 5 gives about 44.7% width and height, or 20% area. The settings placement preview shows the same reduced-size outline, including the configured position, rotation and curvature. The frames disappear in game when adjustment ends; no inner frame is shown when adaptive sizing is disabled or the anchor is Head Locked.

Developers building Windows packages must first restore the pinned native compiler with `python -m pip install --target build\openxr-gaze-tools -r native\openxr-gaze\requirements.txt`, then run `npm run build:vr-gaze`. The build downloads headers from a pinned OpenXR SDK revision and includes the tracking DLL in Windows packages. `npm run test:vr-openxr` tests the compiled layer against a simulated host and verifies actual cross-process pose transfer; it does not replace testing EDVR/VDXR in a physical headset.

In **Advanced Settings > Overlay**, select **VR Overlay** to adjust the window's placement. The live preview shows an empty orange forward-view frame with horizontal and vertical center axes, and the overlay window in blue. It updates as you adjust the horizontal and vertical offsets, distance, pitch, yaw, size, and curvature, without requiring a connected VR headset.

The controls and preview update locally while you drag. Configuration saves are grouped after 200 ms without movement, or immediately when you release the slider, so saving does not queue up old slider positions.

The preview uses an illustrative projection, not your headset's exact field of view. The blue window's width, height, and displacement from the center axes are shown at half scale to keep adjustments within the reference frame more easily; this does not change the actual VR size or placement. Parts of the blue window outside the orange frame are outside this reference view. For **World Locked**, the reference viewer faces forward at an eye height of 1.4 m; moving your headset changes the actual view.

## Plugins

Explore [Plugins](./plugins/index.md) to enhance your experience: fast, on-device speech-to-text (STT), text-to-speech (TTS), and embedding.

## Upgrading the AI

You will automatically receive a notification when a new version of the AI is available during the launch of the application. After downloading the new version, run the MSI installer. The one-click installer updates your existing installation; your configuration and conversations are migrated automatically.
