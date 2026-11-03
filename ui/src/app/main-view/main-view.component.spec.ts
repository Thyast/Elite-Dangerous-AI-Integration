import { ComponentFixture, TestBed } from "@angular/core/testing";
import { NO_ERRORS_SCHEMA } from "@angular/core";
import { By } from "@angular/platform-browser";
import { CommonModule } from "@angular/common";
import { NoopAnimationsModule } from "@angular/platform-browser/animations";
import { ActivatedRoute } from "@angular/router";
import { MatSnackBar } from "@angular/material/snack-bar";
import { BehaviorSubject, of, Subject } from "rxjs";
import { MainViewComponent } from "./main-view.component";
import { Config, ConfigService } from "../services/config.service";
import { TauriService } from "../services/tauri.service";
import { LoggingService } from "../services/logging.service";
import { ChatService } from "../services/chat.service";
import { ProjectionsService } from "../services/projections.service";
import { MetricsService } from "../services/metrics.service";
import { PolicyService } from "../services/policy.service";
import { UIMessage, UISectionRequest, UIService } from "../services/ui.service";
import { PluginUIService } from "../services/plugin-ui.service";
import { RegisteredUIView } from "../services/plugin-ui";
import { TranslateModule } from "@ngx-translate/core";

describe("full VR interface lifecycle and voice navigation", () => {
    let fixture: ComponentFixture<MainViewComponent>;
    let tauri: jasmine.SpyObj<TauriService>;
    let configs: BehaviorSubject<Config | null>;
    let views: BehaviorSubject<RegisteredUIView[]>;
    let messages: BehaviorSubject<UIMessage | null>;
    let visibility: BehaviorSubject<boolean>;
    let adjusting: BehaviorSubject<boolean>;
    let sectionRequests: Subject<UISectionRequest>;
    let shipInfo: BehaviorSubject<{ ShipIdent: string } | null>;
    let navigate: jasmine.Spy;
    let notify: jasmine.Spy;
    let storedShowEvents: string | null;
    const routeData = { overlayInterface: true };
    const plan: RegisteredUIView = {
        id: "upgrades", target: "plugin:ship:upgrades", pluginGuid: "ship",
        label: "Ship upgrade plan", sections: [{ key: "plan", label: "Plan" }],
    };

    beforeEach(async () => {
        storedShowEvents = localStorage.getItem("covas.ui.chat-show-events");
        localStorage.removeItem("covas.ui.chat-show-events");
        configs = new BehaviorSubject<Config | null>(null);
        views = new BehaviorSubject<RegisteredUIView[]>([]);
        messages = new BehaviorSubject<UIMessage | null>(null);
        visibility = new BehaviorSubject(true);
        adjusting = new BehaviorSubject(false);
        sectionRequests = new Subject<UISectionRequest>();
        shipInfo = new BehaviorSubject<{ ShipIdent: string } | null>(null);
        navigate = jasmine.createSpy("showTab");
        notify = jasmine.createSpy("open");
        routeData.overlayInterface = true;
        tauri = jasmine.createSpyObj<TauriService>("TauriService", [
            "runExe", "checkForUpdates", "send_command", "createOverlay", "send_start_signal",
        ]);
        tauri.runExe.and.resolveTo([]);
        tauri.send_command.and.resolveTo();
        tauri.createOverlay.and.resolveTo();
        tauri.send_start_signal.and.resolveTo();
        Object.defineProperty(tauri, "runMode$", { value: of("running") });
        await TestBed.configureTestingModule({
            imports: [MainViewComponent, NoopAnimationsModule, TranslateModule.forRoot()],
            providers: [
                { provide: ActivatedRoute, useValue: { snapshot: { data: routeData } } },
                { provide: TauriService, useValue: tauri },
                { provide: ConfigService, useValue: { config$: configs, system$: of(null) } },
                { provide: LoggingService, useValue: { clearLogs: () => {} } },
                { provide: ChatService, useValue: { clearChat: () => {} } },
                { provide: ProjectionsService, useValue: {
                    inCombat$: of(false), currentStatus$: of(null), shipInfo$: shipInfo,
                } },
                { provide: MetricsService, useValue: {} },
                { provide: PolicyService, useValue: { usageDisclaimerAccepted$: of(true) } },
                { provide: UIService, useValue: {
                    changeUI$: messages, showTab: navigate, overlayInterfaceVisible$: visibility,
                    vrAdjustmentActive$: adjusting,
                    sectionRequests$: sectionRequests,
                } },
                { provide: PluginUIService, useValue: { views$: views } },
                { provide: MatSnackBar, useValue: { open: notify } },
            ],
        }).overrideComponent(MainViewComponent, { set: {
            imports: [CommonModule, TranslateModule], schemas: [NO_ERRORS_SCHEMA],
        } })
            .overrideProvider(MatSnackBar, { useValue: { open: notify } }).compileComponents();
    });

    afterEach(() => {
        if (storedShowEvents === null) {
            localStorage.removeItem("covas.ui.chat-show-events");
        } else {
            localStorage.setItem("covas.ui.chat-show-events", storedShowEvents);
        }
    });

    function create(): MainViewComponent {
        fixture = TestBed.createComponent(MainViewComponent);
        fixture.detectChanges();
        return fixture.componentInstance;
    }

    it("attaches to the existing assistant rather than launching or updating it again", () => {
        const component = create();
        expect(component.isOverlayInterface).toBeTrue();
        expect(tauri.runExe).not.toHaveBeenCalled();
        expect(tauri.checkForUpdates).not.toHaveBeenCalled();
        expect(tauri.send_command).toHaveBeenCalledOnceWith(jasmine.objectContaining({ type: "init_overlay" }));
    });

    it("keeps only the Events filter in the runtime chat without grouping controls", () => {
        create();
        const root: HTMLElement = fixture.nativeElement;
        expect(root.querySelector(".runtime-container .chat-filter-anchor")).not.toBeNull();
        expect(root.querySelector(".runtime-container .chat-events-label")).not.toBeNull();
        expect(root.querySelector(".runtime-container mat-slide-toggle")).toBeNull();
        expect(root.querySelector(".runtime-container mat-select")).toBeNull();
        expect(root.querySelector(".runtime-container .chat-settings-panel")).toBeNull();
        const controls: HTMLElement = fixture.debugElement.query(By.css(".chat-overlay-controls")).nativeElement;
        const style = getComputedStyle(controls);
        expect(style.borderBottomLeftRadius).toBe("8px");
        expect(style.borderTopLeftRadius).toBe("0px");
        expect(style.borderBottomRightRadius).toBe("0px");
        expect(style.backgroundColor).toBe("rgb(22, 22, 22)");
    });

    it("remembers the Events filter and passes it to the chat across view recreation", () => {
        const component = create();
        expect(component.showChatEvents).toBeTrue();
        component.setShowChatEvents(false);
        fixture.detectChanges();
        expect(localStorage.getItem("covas.ui.chat-show-events")).toBe("false");
        expect(fixture.debugElement.query(By.css(".chat-panel app-chat-container")).properties["showEvents"]).toBeFalse();
        fixture.destroy();

        const reopened = create();
        expect(reopened.showChatEvents).toBeFalse();
        reopened.setShowChatEvents(true);
        fixture.detectChanges();
        expect(localStorage.getItem("covas.ui.chat-show-events")).toBe("true");
        expect(fixture.debugElement.query(By.css(".chat-panel app-chat-container")).properties["showEvents"]).toBeTrue();
    });

    it("acknowledges an expansion only from the desktop and reports missing sections without claiming success", () => {
        routeData.overlayInterface = false;
        create();
        const button = document.createElement("button");
        button.dataset["uiSection"] = "Example";
        button.setAttribute("aria-expanded", "false");
        button.onclick = () => button.setAttribute("aria-expanded", "true");
        fixture.nativeElement.appendChild(button);
        sectionRequests.next({request_id: "expand", expanded: true, index: 1});
        expect(tauri.send_command).toHaveBeenCalledWith(jasmine.objectContaining({
            type: "ui_section_result", request_id: "expand", success: true, expanded: true,
        }));
        spyOn(console, "error");
        sectionRequests.next({request_id: "missing", expanded: false, name: "Missing"});
        expect(tauri.send_command).toHaveBeenCalledWith(jasmine.objectContaining({
            type: "ui_section_result", request_id: "missing", success: false,
        }));
        fixture.destroy();
        routeData.overlayInterface = true;
        create();
        tauri.send_command.calls.reset();
        sectionRequests.next({request_id: "headset", expanded: true, index: 1});
        expect(tauri.send_command).not.toHaveBeenCalled();
    });

    it("retains normal startup in the desktop window", () => {
        routeData.overlayInterface = false;
        create();
        expect(tauri.runExe).toHaveBeenCalledTimes(1);
        expect(tauri.checkForUpdates).toHaveBeenCalledTimes(1);
        expect(tauri.send_command).not.toHaveBeenCalled();
    });

    it("honors voice requests arriving before a plugin has published its views", () => {
        const component = create();
        messages.next({ type: "ui", timestamp: "", index: 0, show: plan.target, submenu: "plan" });
        expect(component.selectedTabIndex).toBe(0);
        views.next([plan]);
        expect(component.selectedTabIndex).toBe(5);
        shipInfo.next({ ShipIdent: "TEST" });
        expect(component.selectedTabIndex).toBe(8);
        expect(navigate).not.toHaveBeenCalled();
    });

    it("selects built-in tabs on voice commands and broadcasts manual tab changes", () => {
        const component = create();
        messages.next({ type: "ui", timestamp: "", index: 0, show: "navigation", submenu: "route" });
        expect(component.selectedTabIndex).toBe(1);
        component.onTabSelected(1);
        expect(navigate).not.toHaveBeenCalled();
        component.onTabSelected(2);
        expect(navigate).toHaveBeenCalledOnceWith("logbook");
    });

    it("passes full-interface content to Electron even with the lightweight overlay switches off", async () => {
        routeData.overlayInterface = false;
        const component = create();
        component.config = {
            overlay_mode: "vr", overlay_vr_content: "interface",
            overlay_vr_gaze_zoom: true, overlay_vr_gaze_reduction_factor: 5, overlay_vr_anchor: "world",
            overlay_show_avatar: false, overlay_show_chat: false, overlay_show_hud: false,
        } as Config;
        await component.start();
        expect(tauri.createOverlay).toHaveBeenCalledOnceWith(jasmine.objectContaining({
            mode: "vr", vrContent: "interface", vrGazeZoom: true, vrGazeReductionFactor: 5, vrAnchor: "world",
        }));
        expect(tauri.send_start_signal).toHaveBeenCalled();
    });

    it("hides only the VR GUI and restores its selected plugin tab without restarting", () => {
        const component = create();
        views.next([plan]);
        messages.next({ type: "ui", timestamp: "", index: 0, show: plan.target, submenu: "plan" });
        const selected = component.selectedTabIndex;
        visibility.next(false);
        expect(component.showOverlayChat).toBeTrue();
        expect(document.documentElement.classList.contains("overlay-window")).toBeTrue();
        expect(component.selectedTabIndex).toBe(selected);
        visibility.next(true);
        expect(component.showOverlayChat).toBeFalse();
        expect(document.documentElement.classList.contains("overlay-window")).toBeFalse();
        expect(component.selectedTabIndex).toBe(selected);
        expect(tauri.send_command).toHaveBeenCalledTimes(1);
        expect(tauri.createOverlay).not.toHaveBeenCalled();
        expect(tauri.send_start_signal).not.toHaveBeenCalled();
    });

    it("keeps the GUI hidden when a tab is selected and ignores visibility on the desktop", () => {
        visibility.next(false);
        const component = create();
        messages.next({ type: "ui", timestamp: "", index: 0, show: "navigation" });
        expect(component.selectedTabIndex).toBe(1);
        expect(component.showOverlayChat).toBeTrue();
        fixture.destroy();
        expect(document.documentElement.classList.contains("overlay-window")).toBeFalse();
        routeData.overlayInterface = false;
        const desktop = create();
        expect(desktop.showOverlayChat).toBeFalse();
        expect(desktop.overlayInterfaceVisible).toBeTrue();
    });

    it("does not create a lightweight overlay if all of its content is disabled", async () => {
        routeData.overlayInterface = false;
        const component = create();
        component.config = {
            overlay_mode: "vr", overlay_vr_content: "hud",
            overlay_show_avatar: false, overlay_show_chat: false, overlay_show_hud: false,
        } as Config;
        await component.start();
        expect(tauri.createOverlay).not.toHaveBeenCalled();
    });

    it("draws non-interactive full and reduced frames only during VR adjustment and updates the actual area live", () => {
        configs.next({
            overlay_vr_anchor: "world", overlay_vr_gaze_zoom: true, overlay_vr_gaze_reduction_factor: 5,
        } as Config);
        create();
        const root: HTMLElement = fixture.nativeElement;
        expect(root.querySelector(".vr-adjustment-frame")).toBeNull();
        adjusting.next(true);
        fixture.detectChanges();
        const outer = root.querySelector(".vr-adjustment-frame")!;
        expect(getComputedStyle(outer).pointerEvents).toBe("none");
        expect(getComputedStyle(outer).position).toBe("fixed");
        for (const factor of [1, 5, 9, 12.5]) {
            configs.next({ ...configs.value!, overlay_vr_gaze_reduction_factor: factor });
            fixture.detectChanges();
            const inner = root.querySelector<HTMLElement>(".vr-reduced-frame")!;
            const width = parseFloat(inner.style.width) / 100;
            const height = parseFloat(inner.style.height) / 100;
            expect(width).toBeCloseTo(1 / Math.sqrt(factor), 6);
            expect(width * height).toBeCloseTo(1 / factor, 6);
            const fullBounds = outer.getBoundingClientRect();
            const reducedBounds = inner.getBoundingClientRect();
            expect(reducedBounds.width / fullBounds.width).toBeCloseTo(width, 4);
            expect(reducedBounds.height / fullBounds.height).toBeCloseTo(height, 4);
            expect(getComputedStyle(inner).borderTopColor).toBe("rgb(66, 165, 245)");
            expect(getComputedStyle(inner).borderTopStyle).toBe("dashed");
        }
        adjusting.next(false);
        fixture.detectChanges();
        expect(root.querySelector(".vr-adjustment-frame")).toBeNull();
    });

    it("does not draw a reduced frame for head locking, disabled adaptive sizing or invalid factors", () => {
        const component = create();
        adjusting.next(true);
        for (const changes of [
            { overlay_vr_anchor: "head" },
            { overlay_vr_gaze_zoom: false },
            ...[0, NaN, Infinity].map(factor => ({ overlay_vr_gaze_reduction_factor: factor })),
        ]) {
            configs.next({
                overlay_vr_anchor: "world", overlay_vr_gaze_zoom: true, overlay_vr_gaze_reduction_factor: 5,
                ...changes,
            } as Config);
            fixture.detectChanges();
            expect(fixture.nativeElement.querySelector(".vr-reduced-frame")).toBeNull();
        }
        fixture.destroy();
        adjusting.next(false);
        expect(component.vrAdjustmentActive).toBeTrue();
        routeData.overlayInterface = false;
        create();
        fixture.detectChanges();
        expect(fixture.nativeElement.querySelector(".vr-adjustment-frame")).toBeNull();
    });

    it("keeps the full interface and assistant running when gaze tracking is unavailable", async () => {
        routeData.overlayInterface = false;
        const component = create();
        component.config = { overlay_mode: "vr", overlay_vr_content: "interface", overlay_vr_gaze_zoom: true } as Config;
        tauri.createOverlay.and.resolveTo({
            gazeZoom: { status: "unavailable", message: "Restart EDVR after installing gaze tracking" },
        });
        await component.start();
        expect(tauri.send_start_signal).toHaveBeenCalled();
        expect(notify).toHaveBeenCalledWith("vrInterface.gazeUnavailable", "OK", { duration: 10000 });
    });
});
