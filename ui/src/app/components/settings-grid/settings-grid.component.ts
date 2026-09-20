import { Component, Input, Output, EventEmitter, inject } from "@angular/core";
import { CommonModule } from "@angular/common";
import { SettingsFieldComponent } from "../settings-field/settings-field.component";
import { SettingsGrid, SettingBase } from "../../services/plugin-settings";
import { TranslateService } from "@ngx-translate/core";
import { resolveSettingText } from "../../services/setting-text";
import { MatExpansionModule } from "@angular/material/expansion";

/**
 * A reusable component for rendering a settings grid with a label and fields.
 * Used by both plugin-settings and advanced-settings for provider settings.
 */
@Component({
    selector: "app-settings-grid",
    standalone: true,
    imports: [
        CommonModule,
        SettingsFieldComponent,
        MatExpansionModule,
    ],
    templateUrl: "./settings-grid.component.html",
    styleUrl: "./settings-grid.component.css",
})
export class SettingsGridComponent {
    private readonly translate = inject(TranslateService);

    /**
     * The settings grid definition containing label and fields.
     */
    @Input() grid!: SettingsGrid;
    
    /**
     * A function to get the current value for a field key.
     * Signature: (fieldKey: string, defaultValue: any) => any
     */
    @Input() getValue!: (fieldKey: string, defaultValue: any) => any;
    
    /**
     * A function to set a new value for a field key.
     * Signature: (fieldKey: string, value: any) => void
     */
    @Input() setValue!: (fieldKey: string, value: any) => void;

    @Input() onButtonClick?: (fieldKey: string) => void;
    
    /**
     * Optional: Header level for the grid label (default: h3).
     * Use 'h2' for main sections, 'h3' for subsections.
     */
    @Input() headerLevel: 'h2' | 'h3' | 'h4' = 'h3';

    /**
     * Optional: CSS class to apply to the grid container.
     */
    @Input() gridClass: string = 'settings-grid';

    onFieldChange(field: SettingBase, value: any): void {
        this.setValue(field.key, value);
    }

    getFieldValue(field: SettingBase): any {
        return this.getValue(field.key, field.default_value);
    }

    resolve(value: string | null | undefined): string {
        return resolveSettingText(this.translate, value);
    }

    handleButtonClick(field: SettingBase): void {
        this.onButtonClick?.(field.key);
    }

    handleListAction(event: { action: string; rowKey: string }): void {
        this.onButtonClick?.(`${event.action}:${event.rowKey}`);
    }
}
