import { ComponentFixture, fakeAsync, flushMicrotasks, TestBed, tick } from "@angular/core/testing";
import { NoopAnimationsModule } from "@angular/platform-browser/animations";
import { TranslateModule, TranslateService } from "@ngx-translate/core";
import { Subject } from "rxjs";
import { TRANSLATIONS } from "../../services/i18n-translations";
import { BaseMessage, TauriService } from "../../services/tauri.service";
import { VoicePreviewComponent } from "./voice-preview.component";

describe("VoicePreviewComponent", () => {
    let fixture: ComponentFixture<VoicePreviewComponent>;
    let component: VoicePreviewComponent;
    let output: Subject<BaseMessage>;
    let tauri: jasmine.SpyObj<TauriService>;

    const result = (requestId: string, status: string, message?: string): BaseMessage => ({
        type: "tts_preview", timestamp: "", index: 0, request_id: requestId, status, message,
    });

    beforeEach(async () => {
        output = new Subject<BaseMessage>();
        tauri = jasmine.createSpyObj<TauriService>("TauriService", ["send_command"], { output$: output });
        tauri.send_command.and.resolveTo();
        await TestBed.configureTestingModule({
            imports: [VoicePreviewComponent, NoopAnimationsModule, TranslateModule.forRoot()],
            providers: [{ provide: TauriService, useValue: tauri }],
        }).compileComponents();
        const translate = TestBed.inject(TranslateService);
        translate.setTranslation("en", TRANSLATIONS["en"]);
        translate.setTranslation("fr", TRANSLATIONS["fr"]);
        translate.use("fr");
        fixture = TestBed.createComponent(VoicePreviewComponent);
        component = fixture.componentInstance;
        component.characterIndex = 1;
        fixture.detectChanges();
    });

    afterEach(() => fixture.destroy());

    it("renders a French test message and sends it from the play button", async () => {
        expect(component.text).toContain("Bonjour Commandant.");
        const button: HTMLButtonElement = fixture.nativeElement.querySelector("button");
        expect(button.textContent).toContain("Écouter la voix");
        button.click();
        expect(tauri.send_command).toHaveBeenCalledOnceWith(jasmine.objectContaining({
            type: "preview_tts", character_index: 1, text: component.text,
        }));
        const requestId = tauri.send_command.calls.mostRecent().args[0]["request_id"];
        output.next(result(requestId, "completed"));
        await fixture.whenStable();
        expect(component.completed).toBe(1);
    });

    it("generates distinct requests for the same text and prevents overlap", async () => {
        const text = component.text;
        await component.play();
        const first = tauri.send_command.calls.mostRecent().args[0]["request_id"];
        await component.play();
        expect(tauri.send_command).toHaveBeenCalledTimes(1);
        output.next(result("unrelated", "completed"));
        expect(component.busy).toBeTrue();
        output.next(result(first, "playing"));
        expect(component.busy).toBeTrue();
        output.next(result(first, "completed"));
        await component.play();
        const second = tauri.send_command.calls.mostRecent().args[0]["request_id"];
        expect(second).not.toBe(first);
        expect(component.text).toBe(text);
        expect(tauri.send_command.calls.mostRecent().args[0]["text"]).toBe(text);
        output.next(result(first, "error", "Late error"));
        expect(component.error).toBe("");
        expect(component.busy).toBeTrue();
        output.next(result(second, "completed"));
        expect(component.completed).toBe(2);
    });

    it("disables invalid requests and uses edited text on retry", async () => {
        component.disabled = true;
        await component.play();
        component.disabled = false;
        component.characterIndex = null;
        await component.play();
        component.characterIndex = 1;
        component.text = " ";
        await component.play();
        component.text = "a".repeat(501);
        await component.play();
        expect(tauri.send_command).not.toHaveBeenCalled();

        component.text = "Une autre phrase.";
        await component.play();
        const requestId = tauri.send_command.calls.mostRecent().args[0]["request_id"];
        output.next(result(requestId, "error", "Quota exceeded"));
        fixture.detectChanges();
        expect(fixture.nativeElement.querySelector('[role="alert"]').textContent).toContain("Quota exceeded");
        expect(component.canPreview).toBeTrue();
        expect(component.completed).toBe(0);
    });

    it("reports transport errors and backend exits without remaining busy", async () => {
        spyOn(console, "error");
        tauri.send_command.and.rejectWith(new Error("Transport unavailable"));
        await component.play();
        expect(component.error).toBe("Transport unavailable");
        expect(component.busy).toBeFalse();
        tauri.send_command.and.resolveTo();
        await component.play();
        output.next({ type: "backend_process_state", timestamp: "", index: 0, status: "exited" });
        expect(component.error).toContain("backend");
        expect(component.busy).toBeFalse();
    });

    it("does not apply an old transport failure to a newer preview", async () => {
        spyOn(console, "error");
        let rejectFirst: (reason: Error) => void = () => fail("Missing pending request");
        tauri.send_command.and.returnValue(new Promise<void>((resolve, reject) => {
            rejectFirst = reject;
        }));
        const firstPending = component.play();
        const firstId = tauri.send_command.calls.mostRecent().args[0]["request_id"];
        output.next(result(firstId, "completed"));
        tauri.send_command.and.resolveTo();
        await component.play();
        rejectFirst(new Error("Old failure"));
        await firstPending;
        expect(component.busy).toBeTrue();
        expect(component.error).toBe("");
    });

    it("reports a missing completion instead of showing success", fakeAsync(() => {
        void component.play();
        flushMicrotasks();
        tick(180_000);
        expect(component.busy).toBeFalse();
        expect(component.completed).toBe(0);
        expect(component.error).toContain("trois minutes");
    }));
});
