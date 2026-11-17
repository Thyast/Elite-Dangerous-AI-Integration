import { ComponentFixture, TestBed } from "@angular/core/testing";
import { BehaviorSubject, of } from "rxjs";
import { OverlayViewComponent } from "./overlay-view.component";
import { TauriService } from "../services/tauri.service";
import { ChatMessage, ChatService } from "../services/chat.service";
import { AvatarService } from "../services/avatar.service";
import { CharacterService } from "../services/character.service";
import { Config, ConfigService } from "../services/config.service";
import { EventMessage, EventService } from "../services/event.service";
import { GenUiService } from "../services/gen-ui.service";

describe("overlay chat-only fallback", () => {
    let fixture: ComponentFixture<OverlayViewComponent>;
    let config: BehaviorSubject<Config | null>;
    let events: BehaviorSubject<EventMessage[]>;
    let send: jasmine.Spy;
    const message: ChatMessage = {
        type: "chat", timestamp: "", index: 0, role: "covas", message: "Ready commander",
    };

    beforeEach(async () => {
        config = new BehaviorSubject<Config | null>(null);
        events = new BehaviorSubject<EventMessage[]>([]);
        send = jasmine.createSpy("send_command").and.resolveTo();
        await TestBed.configureTestingModule({
            imports: [OverlayViewComponent],
            providers: [
                { provide: TauriService, useValue: { output$: of(), send_command: send } },
                { provide: ChatService, useValue: { chatMessage$: of(null), chatHistory$: of([message]) } },
                { provide: AvatarService, useValue: { isObjectUrl: () => false } },
                { provide: CharacterService, useValue: {
                    avatarUrl$: of(null), avatarMime$: of(null), character$: of(null),
                    getCurrentCharacter: () => null, getAvatarUrl: () => null, getAvatarMime: () => null,
                } },
                { provide: ConfigService, useValue: { config$: config } },
                { provide: EventService, useValue: { events$: events } },
                { provide: GenUiService, useValue: { uiCode$: of(null), uiState$: of(null) } },
            ],
        }).compileComponents();
    });

    function create(chatOnly: boolean): OverlayViewComponent {
        fixture = TestBed.createComponent(OverlayViewComponent);
        fixture.componentRef.setInput("chatOnly", chatOnly);
        fixture.detectChanges();
        return fixture.componentInstance;
    }

    function emit(kind: "assistant_speaking" | "assistant_completed"): void {
        events.next([{
            type: "event", timestamp: "", index: 0,
            event: { kind, content: "", timestamp: "", processed_at: 0 },
        }]);
        fixture.detectChanges();
    }

    it("renders only chat even when avatars and generated HUD would normally be displayed", () => {
        const component = create(true);
        emit("assistant_speaking");
        const root: HTMLElement = fixture.nativeElement;
        expect(root.querySelector(".overlay-chat")?.textContent).toContain(message.message);
        expect(root.querySelector(".overlay-pngtuber, .overlay-svg-tuber, app-gen-ui-render")).toBeNull();
        expect(component.avatarShow).toBeTrue();
        expect(send).not.toHaveBeenCalled();
    });

    it("keeps transient chat available with Show Chat off and no TTS, then hides it at rest", () => {
        config.next({ overlay_show_chat: false, tts_provider: "none" } as Config);
        const component = create(true);
        emit("assistant_speaking");
        const root: HTMLElement = fixture.nativeElement;
        const character = root.querySelector<HTMLElement>(".overlay-character")!;
        expect(component.chatShow).toBeFalse();
        expect(root.querySelector(".overlay-chat")?.textContent).toContain(message.message);
        expect(character.classList.contains("idle")).toBeFalse();
        emit("assistant_completed");
        expect(character.classList.contains("idle")).toBeTrue();
        expect(getComputedStyle(character).transitionDuration).toBe("0.26s");
    });

    it("preserves normal standalone overlay initialization, avatars and generated HUD", () => {
        create(false);
        expect(send).toHaveBeenCalledOnceWith(jasmine.objectContaining({ type: "init_overlay" }));
        const root: HTMLElement = fixture.nativeElement;
        expect(root.querySelector(".overlay-pngtuber")).not.toBeNull();
        expect(root.querySelector("app-gen-ui-render")).not.toBeNull();
    });
});
