# Tauri + Angular

This template should help get you started developing with Tauri and Angular.

## Recommended IDE Setup

[VS Code](https://code.visualstudio.com/) + [Tauri](https://marketplace.visualstudio.com/items?itemName=tauri-apps.tauri-vscode) + [rust-analyzer](https://marketplace.visualstudio.com/items?itemName=rust-lang.rust-analyzer) + [Angular Language Service](https://marketplace.visualstudio.com/items?itemName=Angular.ng-template).

## Testing chat event grouping

From this directory, run the focused Jasmine/Karma tests with Chrome installed:

```sh
npm test -- --watch=false --browsers=ChromeHeadless --include=src/app/services/chat.service.spec.ts --include=src/app/components/chat-container/chat-container.component.spec.ts
```

Set `CHROME_BIN` to the browser executable if Karma cannot locate Chrome automatically.
