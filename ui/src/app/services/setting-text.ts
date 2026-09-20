import { TranslateService } from "@ngx-translate/core";

/**
 * Pattern used to distinguish i18n keys from literal strings sent by plugins.
 * A key is dot-separated lowercase segments, e.g. "plugin.sum.btn.confirm".
 * Literal labels (e.g. "Ship Upgrade Manager") do not match and are shown as-is.
 */
const I18N_KEY_PATTERN = /^[a-z][a-z0-9]*(?:\.[a-z0-9_-]+)+$/;

export function looksLikeI18nKey(value: string | null | undefined): boolean {
    return typeof value === "string" && I18N_KEY_PATTERN.test(value);
}

/**
 * Resolves a plugin-provided settings string.
 * Keys are translated; literal strings pass through untouched so plugins
 * that have not been migrated to i18n keep working.
 */
export function resolveSettingText(translate: TranslateService, value: string | null | undefined): string {
    if (!value) {
        return "";
    }
    if (!looksLikeI18nKey(value)) {
        return value;
    }
    const translated = translate.instant(value);
    return typeof translated === "string" ? translated : value;
}
