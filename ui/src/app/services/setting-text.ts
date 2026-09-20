import { TranslateService } from "@ngx-translate/core";

/**
 * Pattern used to distinguish i18n keys from literal strings sent by plugins.
 * A key is dot-separated segments, e.g. "plugin.sum.availablePlans". Segment
 * casing is not restricted so camelCase names resolve as well. Literal
 * labels (e.g. "Ship Upgrade Manager") do not match and are shown as-is.
 */
const I18N_KEY_PATTERN = /^[a-zA-Z][a-zA-Z0-9_-]*(?:\.[a-zA-Z0-9_-]+)+$/;

export function looksLikeI18nKey(value: string | null | undefined): boolean {
    return typeof value === "string" && I18N_KEY_PATTERN.test(value);
}

/**
 * Resolves a plugin-provided settings string.
 * Keys are translated; literal strings pass through untouched so plugins
 * that have not been migrated to i18n keep working.
 */
export function resolveSettingText(
    translate: TranslateService,
    value: string | null | undefined,
    params?: { [name: string]: string | number },
): string {
    if (!value) {
        return "";
    }
    if (!looksLikeI18nKey(value)) {
        return value;
    }
    const translated = translate.instant(value, params);
    return typeof translated === "string" ? translated : value;
}
