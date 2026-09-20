import { Component, Input, Output, EventEmitter, inject } from "@angular/core";
import { CommonModule } from "@angular/common";
import { FormsModule } from "@angular/forms";
import { MatFormFieldModule } from "@angular/material/form-field";
import { MatInputModule } from "@angular/material/input";
import { MatSelectModule } from "@angular/material/select";
import { MatSlideToggleModule } from "@angular/material/slide-toggle";
import { MatOptionModule } from "@angular/material/core";
import { MatButtonModule } from "@angular/material/button";
import { TranslateService } from "@ngx-translate/core";
import { SettingBase } from "../../services/plugin-settings";
import { resolveSettingText } from "../../services/setting-text";

/**
 * A reusable component for rendering plugin/provider settings fields.
 * Supports toggle, paragraph, text, number, textarea, and select field types.
 */
@Component({
    selector: "app-settings-field",
    standalone: true,
    imports: [
        CommonModule,
        FormsModule,
        MatFormFieldModule,
        MatInputModule,
        MatSelectModule,
        MatSlideToggleModule,
        MatOptionModule,
        MatButtonModule,
    ],
    templateUrl: "./settings-field.component.html",
    styleUrl: "./settings-field.component.css",
})
export class SettingsFieldComponent {
    private readonly translate = inject(TranslateService);

    /**
     * The field definition containing type, label, and other metadata.
     */
    @Input() field!: SettingBase;

    /**
     * The current value of the field.
     */
    @Input() value: any;

    @Input() buttonEnabled: boolean = true;

    /**
     * Emitted when the field value changes.
     */
    @Output() valueChange = new EventEmitter<any>();
    @Output() buttonClick = new EventEmitter<void>();

    resolve(value: string | null | undefined): string {
        return resolveSettingText(this.translate, value);
    }

    optionLabel(option: { label?: string | null }): string {
        return this.resolve(option?.label);
    }

    onValueChange(newValue: any): void {
        this.valueChange.emit(newValue);
    }

    onButtonClick(): void {
        this.buttonClick.emit();
    }
}
