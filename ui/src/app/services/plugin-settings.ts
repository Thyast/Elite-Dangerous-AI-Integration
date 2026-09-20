import { type BaseMessage } from "./tauri.service";

export interface PluginSettings {
    key: string;
    label: string;
    icon: string;
    grids: SettingsGrid[];
}

export interface GridHeaderAction {
    key: string;
    label?: string;
    icon?: string;
}

export interface SettingsGrid {
    key: string;
    label: string;
    collapsible?: boolean;
    default_collapsed?: boolean;
    header_action?: GridHeaderAction;
    fields: (TextSetting | TextAreaSetting | NumericalSetting | ToggleSetting | SelectSetting | ButtonSetting | ParagraphSetting | ErrorSetting | ListSetting | FilterSetting)[];
}

export interface SettingBase {
    key: string;
    label: string;
    type: "paragraph" | "number" | "toggle" | "text" | "textarea" | "select" | "button" | "list" | "filter" | "error";
    readonly: boolean | null;
    placeholder: string | null;
    icon?: string;
    default_value?: any;
    params?: { [name: string]: string | number };

    // Paragraph & Error
    content: string;

    // Text & Textarea
    max_length: number | null;
    min_length: number | null;
    hidden: boolean | null;

    // Textarea
    rows: number | null;
    cols: number | null;

    // Numbers
    min_value: number | null;
    max_value: number | null;
    step: number | null;

    // Select
    select_options: SelectOption[];
    multi_select: boolean;

    // List
    items: ListRow[];
    row_actions: ListAction[];
}

export interface TextSetting extends SettingBase {
    default_value: string | null;
}

export interface FilterSetting extends SettingBase {}

export interface TextAreaSetting extends SettingBase {
    default_value: string | string[] | null;
}

export interface NumericalSetting extends SettingBase {
    default_value: number | null;
}

export interface ToggleSetting extends SettingBase {
    default_value: boolean | null;
}

export interface ButtonSetting extends SettingBase {
    icon?: string;
}

export interface ParagraphSetting extends SettingBase {}

export interface ErrorSetting extends SettingBase {}

export interface ListAction {
    action: string;
    icon?: string;
    label?: string;
    danger?: boolean;
    inline_edit?: boolean;
}

export interface ListRowProgress {
    ship?: string;
    ship_model?: string;
    paused?: boolean;
    modules_done?: number;
    modules_total?: number;
    modules_pct?: number;
    eng_current?: number;
    eng_target?: number;
    eng_pct?: number;
    next_label?: string;
    next_grade?: number | string;
    next_engineering?: string;
}

export interface ListRow {
    key: string;
    title: string;
    meta?: string;
    group?: string;
    progress?: ListRowProgress[];
}

export interface ListSetting extends SettingBase {}

export interface SelectSetting extends SettingBase {
    default_value: string | string[] | null;
}

export interface SelectOption {
    key: string;
    label: string;
    value: object | string | number | boolean;
    disabled: boolean;
}

export interface PluginSettingsMap {
    [plugin_guid: string]: PluginSettings;
}

export interface PluginSettingsMessage extends BaseMessage {
    type: "plugin_settings_configs";
    plugin_settings_configs: PluginSettingsMap;
    has_plugin_settings: boolean;
}

export interface ModelProviderDefinition {
    kind: 'llm' | 'vlm' | 'stt' | 'tts' | 'embedding';
    id: string;
    label: string;
    settings_config: SettingsGrid[];
    plugin_guid: string;
    is_builtin: boolean;
}

export interface PluginModelProvidersMessage extends BaseMessage {
    type: "plugin_model_providers";
    providers: ModelProviderDefinition[];
}
