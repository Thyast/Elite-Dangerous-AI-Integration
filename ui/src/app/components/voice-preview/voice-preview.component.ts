import { Component, Input, OnDestroy } from "@angular/core";
import { FormsModule } from "@angular/forms";
import { MatButtonModule } from "@angular/material/button";
import { MatFormFieldModule } from "@angular/material/form-field";
import { MatIconModule } from "@angular/material/icon";
import { MatInputModule } from "@angular/material/input";
import { TranslateModule, TranslateService } from "@ngx-translate/core";
import { Subscription } from "rxjs";
import { BaseMessage, TauriService } from "../../services/tauri.service";

interface VoicePreviewMessage extends BaseMessage {
    type: "tts_preview";
    request_id: string;
    status: "playing" | "completed" | "error";
    message?: string;
}

@Component({
    selector: "app-voice-preview",
    standalone: true,
    imports: [FormsModule, MatButtonModule, MatFormFieldModule, MatIconModule, MatInputModule, TranslateModule],
    template: `
        <section [attr.aria-busy]="busy">
            <mat-form-field>
                <mat-label>{{ 'voicePreview.message' | translate }}</mat-label>
                <textarea matInput [(ngModel)]="text" rows="3" maxlength="500" [disabled]="busy"></textarea>
            </mat-form-field>
            <button mat-raised-button type="button" [disabled]="!canPreview" (click)="play()">
                <mat-icon>play_arrow</mat-icon>
                {{ (busy ? 'voicePreview.playing' : 'voicePreview.play') | translate }}
            </button>
            <p>{{ 'voicePreview.help' | translate }}</p>
            @if (completed > 0) {
                <p role="status">{{ 'voicePreview.completed' | translate:{ count: completed } }}</p>
            }
            @if (error) {
                <p role="alert">{{ 'voicePreview.failed' | translate }} {{ error }}</p>
            }
        </section>
    `,
    styles: [`
        :host { display: block; margin: 16px 0; }
        mat-form-field { display: block; width: 100%; }
        p { font-size: 0.875rem; }
        [role="alert"] { color: var(--mat-form-field-error-text-color, #f44336); }
    `],
})
export class VoicePreviewComponent implements OnDestroy {
    @Input() characterIndex: number | null = null;
    @Input() disabled = false;
    text: string;
    busy = false;
    completed = 0;
    error = "";
    private requestId: string | null = null;
    private timer?: ReturnType<typeof setTimeout>;
    private readonly subscription: Subscription;

    constructor(private tauri: TauriService, private translate: TranslateService) {
        this.text = this.translate.instant("voicePreview.sample");
        this.subscription = this.tauri.output$.subscribe(message => {
            if (!this.requestId) return;
            if (message.type === "backend_process_state") {
                this.fail(this.translate.instant("voicePreview.disconnected"));
            } else if (message.type === "tts_preview") {
                const preview = message as VoicePreviewMessage;
                if (preview.request_id !== this.requestId) return;
                if (preview.status === "completed") {
                    this.completed++;
                    this.finish();
                } else if (preview.status === "error") {
                    this.fail(preview.message || this.translate.instant("voicePreview.failed"));
                }
            }
        });
    }

    get canPreview(): boolean {
        return !this.disabled && !this.busy && this.characterIndex !== null
            && this.text.trim().length > 0 && this.text.length <= 500;
    }

    async play(): Promise<void> {
        if (!this.canPreview) return;
        this.busy = true;
        this.error = "";
        const requestId = crypto.randomUUID();
        this.requestId = requestId;
        this.timer = setTimeout(() => this.fail(this.translate.instant("voicePreview.timeout")), 180_000);
        try {
            await this.tauri.send_command({
                type: "preview_tts",
                timestamp: new Date().toISOString(),
                request_id: requestId,
                character_index: this.characterIndex,
                text: this.text,
            });
        } catch (error) {
            console.error("Could not request voice preview", error);
            if (this.requestId === requestId) {
                this.fail(error instanceof Error ? error.message : this.translate.instant("voicePreview.failed"));
            }
        }
    }

    private fail(message: string): void {
        this.error = message;
        this.finish();
    }

    private finish(): void {
        clearTimeout(this.timer);
        this.requestId = null;
        this.busy = false;
    }

    ngOnDestroy(): void {
        clearTimeout(this.timer);
        this.subscription.unsubscribe();
    }
}
