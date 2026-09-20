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
import { MatIconModule } from "@angular/material/icon";
import { MatProgressBarModule } from "@angular/material/progress-bar";
import { SettingBase, ListAction, ListRow } from "../../services/plugin-settings";
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
        MatIconModule,
        MatProgressBarModule,
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

    private readonly collapsedGroups = new Set<string>();

    /**
     * Emitted when the field value changes.
     */
    @Output() valueChange = new EventEmitter<any>();
    @Output() buttonClick = new EventEmitter<void>();
    @Output() listAction = new EventEmitter<{ action: string; rowKey: string; value?: string }>();

    editingRowKey: string | null = null;
    editingAction: ListAction | null = null;
    editValue = "";

    resolve(
        value: string | null | undefined,
        params?: { [name: string]: string | number },
    ): string {
        return resolveSettingText(this.translate, value, params);
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

    onListAction(action: ListAction, row: ListRow): void {
        if (action.inline_edit) {
            this.editingAction = action;
            this.editingRowKey = row.key;
            this.editValue = row.title;
            return;
        }
        this.listAction.emit({ action: action.action, rowKey: row.key });
    }

    confirmRowEdit(row: ListRow): void {
        const action = this.editingAction;
        this.editingRowKey = null;
        this.editingAction = null;
        if (!action) {
            return;
        }
        this.listAction.emit({ action: action.action, rowKey: row.key, value: this.editValue });
    }

    cancelRowEdit(): void {
        this.editingRowKey = null;
        this.editingAction = null;
        this.editValue = "";
    }

    rowsWithGroups(items: ListRow[] | null | undefined): { row: ListRow; header?: string; count?: number }[] {
        const counts = new Map<string, number>();
        for (const row of items ?? []) {
            if (row.group) {
                counts.set(row.group, (counts.get(row.group) ?? 0) + 1);
            }
        }
        const entries: { row: ListRow; header?: string; count?: number }[] = [];
        let lastGroup: string | undefined = undefined;
        for (const row of items ?? []) {
            const header = row.group && row.group !== lastGroup ? row.group : undefined;
            lastGroup = row.group ?? lastGroup;
            entries.push({
                row,
                header,
                count: header !== undefined ? counts.get(row.group as string) : undefined,
            });
        }
        return entries;
    }

    toggleGroup(group: string): void {
        if (this.collapsedGroups.has(group)) {
            this.collapsedGroups.delete(group);
        } else {
            this.collapsedGroups.add(group);
        }
    }

    isGroupCollapsed(group: string | undefined): boolean {
        return group !== undefined && this.collapsedGroups.has(group);
    }

    groupCountLabel(entry: { header?: string; count?: number }): string {
        if (entry.header === undefined || entry.count === undefined) {
            return "";
        }
        const key = entry.count === 1 ? "plugin.sum.groupOne" : "plugin.sum.groupMany";
        return this.resolve(key, { count: entry.count });
    }
}
