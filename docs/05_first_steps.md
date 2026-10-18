
# First Steps

If you have not already done so, you should first complete the [Getting Started](./index.md) guide. This guide will help you set up your AI and get you started with the basics.

Once you have completed the setup, you can start using the AI in-game. The AI can help you with a variety of tasks, such as navigating the galaxy map, managing your ship's systems, and providing information about the game world. The AI can also assist you in combat, mining, trading, and other activities.

There are several configuration options, that you can adjust to customize the AI to your liking.

## Character Prompt

The AI Character Prompt is a description of the AI's personality. You can modify this text to your liking – if you want the AI to talk about your ships cat, you can mention "We have a cat on our ship that likes to get in the way of my controls when it wants attention.", causing the AI to role-play and mention the cats current whereabouts regularly. More examples can be found on our discord server.

## Audio

The integration uses your microphone for speech recognition. We support voice activation and push-to-talk (PTT). When using voice activation, we recommend using headphones to avoid the AI hearing itself, which causes a feedback loop.

### Google AI Studio text-to-speech

Select **Google AI Studio** as the TTS / Speaker provider and enter your Google AI Studio API key. The default speech model is **`gemini-3.8-flash-lite-tts`**. You can set a separate TTS API key or change the model in the provider settings. This integration uses the native Gemini Interactions API, not Google's OpenAI-compatible endpoint.

In the character settings, choose a voice (default: **Kore**) and optionally enter **Voice Instructions**, for example, "Speak French with a calm, warm tone and a measured pace." These directions are sent as structured speech metadata, separately from the words to speak. Use these instructions to control pacing; the legacy numeric speech-speed setting is not sent to this API.

Audio streams as raw 16-bit little-endian PCM at 24 kHz, mono, matching the application playback and audio effects pipeline. The integration requests `audio/l16` explicitly to avoid playing a WAV header as audio. Requests use `store: false` rather than saving Interactions history. Selecting a Google API key does not automatically replace your TTS provider; Google TTS is an explicit opt-in.

According to [Google's model documentation](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash-lite-tts), Flash-Lite TTS prioritizes latency, throughput and cost, supports French, and shares its API schema with **`gemini-3.8-flash-tts`**. The latter prioritizes voice fidelity, expressive acting and difficult pronunciations, and can be selected in the same model field. These are the vendor's positioning claims, not an in-app listening benchmark.

Model access, quotas and billing depend on your Google project; an existing STT key does not guarantee access to this TTS model. HTTP errors (including 403/429), stream errors and malformed audio are reported rather than silently falling back to another provider. For a subjective quality check, compare the same French phrases and voice in [Google AI Studio's speech playground](https://aistudio.google.com/generate-speech), including names, numbers and acronyms used in-game.

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

## Plugins

Explore [Plugins](./plugins/index.md) to enhance your experience: fast, on-device speech-to-text (STT), text-to-speech (TTS), and embedding.

## Upgrading the AI

You will automatically receive a notification when a new version of the AI is available during the launch of the application. After downloading the new version, run the MSI installer. The one-click installer updates your existing installation; your configuration and conversations are migrated automatically.
