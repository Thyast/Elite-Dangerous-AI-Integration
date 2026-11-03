import { ComponentFixture, fakeAsync, flushMicrotasks, TestBed, tick } from "@angular/core/testing";
import { By } from "@angular/platform-browser";
import { NoopAnimationsModule } from "@angular/platform-browser/animations";
import { MatDialog } from "@angular/material/dialog";
import { MatSlider, MatSliderThumb } from "@angular/material/slider";
import { MatSlideToggle } from "@angular/material/slide-toggle";
import { MatSelect } from "@angular/material/select";
import { MatSnackBar } from "@angular/material/snack-bar";
import { TranslateModule, TranslateService } from "@ngx-translate/core";
import { BehaviorSubject, of, Subject } from "rxjs";
import { AdvancedSettingsComponent } from "./advanced-settings.component";
import { Config, ConfigService } from "../../services/config.service";
import { CharacterService } from "../../services/character.service";
import { ConfigBackupService } from "../../services/config-backup.service";
import { OverlayRuntimeInfo, TauriService } from "../../services/tauri.service";
import { ChatEventGroupingOptions, ChatService } from "../../services/chat.service";
import { TRANSLATIONS } from "../../services/i18n-translations";
import { FontScaleService } from "../../services/font-scale.service";
import { ApiKeyDetectionService } from "../../services/api-key-detection.service";
import { PluginUIService } from "../../services/plugin-ui.service";

describe("VR slider configuration updates", () => {
    let fixture: ComponentFixture<AdvancedSettingsComponent>;
    let component: AdvancedSettingsComponent;
    let configs: BehaviorSubject<Config | null>;
    let changeConfig: jasmine.Spy;
    let notify: jasmine.Spy;
    let gazeAction: jasmine.Spy;
    let getRuntime: jasmine.Spy;
    let overlayChanges: Subject<void>;
    let chatGrouping: ChatEventGroupingOptions;
    let setChatGrouping: jasmine.Spy;

    const initialConfig: Config = {
        api_key: "", commander_name: "Preview", config_version: 24,
        characters: [], active_character_index: 0,
        llm_provider: "none", agent_llm_provider: "none", agent_llm_max_tries: 7, mute_search: false,
        vision_provider: "none", stt_provider: "none", stt_required_word: "", tts_provider: "none",
        embedding_provider: "none", vision_var: false, ptt_var: "voice_activation",
        wake_word_timeout: 10, wake_word_source: "main", wake_word_first_command: "retranscribe",
        ptt_inverted_var: false, mute_during_response_var: false, game_actions_var: true,
        web_search_actions_var: true, ui_actions_var: true, use_action_cache_var: true,
        allowed_actions: {}, discovery_primary_var: true, discovery_firegroup_var: 1,
        weapon_types: [], prefer_primary_bindings: false, in_system_navigation: true,
        chat_local_tabbed_var: false, chat_wing_tabbed_var: false, chat_system_tabbed_var: true,
        chat_squadron_tabbed_var: false, chat_direct_tabbed_var: false,
        ptt_key: "", ptt_key_secondary: "", input_device_name: "", output_device_name: "",
        output_volume_multiplier: 1, cn_autostart: false, ed_journal_path: "", ed_appdata_path: "",
        qol_autobrake: false, qol_autoscan: false, qol_autoboost: false,
        overlay_show_avatar: true, overlay_show_hud: false, overlay_show_chat: true,
        overlay_mode: "vr", overlay_position: "right", overlay_screen_id: -1,
        overlay_standalone_transparent: true, overlay_standalone_background_color: "#000000",
        overlay_vr_size_meters: 0.9, overlay_vr_anchor: "head",
        overlay_vr_content: "hud",
        overlay_vr_gaze_zoom: false, overlay_vr_gaze_reduction_factor: 5,
        overlay_vr_horizontal_offset: 0, overlay_vr_vertical_offset: 0,
        overlay_vr_distance_offset: 0, overlay_vr_tilt_degrees: 0,
        overlay_vr_yaw_degrees: 0, overlay_vr_curvature: 0, plugin_settings: {},
    };

    function runtimeInfo(config = initialConfig): OverlayRuntimeInfo {
        return {
            platform: "win32", probeMode: "test", openxrAvailable: true,
            openxrOverlayExtensionAvailable: false, openvrAvailable: false,
            openvrRuntimeInstalled: false, openvrRuntimePath: "", selectedBackend: "openxr",
            packageInstalled: true, available: true, hasRealVRRuntime: true,
            desktopOverlay: {
                backend: "win32", source: "platform-default", confidence: "certain", evidence: "test", capabilities: null,
            },
            activeVrSettings: {
                alwaysOnTop: true, mode: "vr",
                vrAnchor: config.overlay_vr_anchor, vrContent: config.overlay_vr_content,
                vrGazeZoom: config.overlay_vr_gaze_zoom, vrGazeReductionFactor: config.overlay_vr_gaze_reduction_factor,
                vrSizeMeters: config.overlay_vr_size_meters,
                vrHorizontalOffset: config.overlay_vr_horizontal_offset, vrVerticalOffset: config.overlay_vr_vertical_offset,
                vrDistanceOffset: config.overlay_vr_distance_offset, vrTiltDegrees: config.overlay_vr_tilt_degrees,
                vrYawDegrees: config.overlay_vr_yaw_degrees, vrCurvature: config.overlay_vr_curvature,
            },
        };
    }

    beforeEach(async () => {
        chatGrouping = { enabled: false, delaySeconds: 2 };
        setChatGrouping = jasmine.createSpy("setEventGrouping").and.callFake(
            (enabled: boolean, delaySeconds: number) => {
                chatGrouping = { enabled, delaySeconds };
            },
        );
        configs = new BehaviorSubject<Config | null>({ ...initialConfig });
        changeConfig = jasmine.createSpy("changeConfig").and.resolveTo();
        notify = jasmine.createSpy("open");
        gazeAction = jasmine.createSpy("runOpenXrGazeIntegrationAction").and.resolveTo({
            supported: true, available: true, installed: true, enabled: true, restartRequired: false,
        });
        getRuntime = jasmine.createSpy("getOverlayRuntimeInfo").and.resolveTo(runtimeInfo());
        overlayChanges = new Subject<void>();
        spyOn(AdvancedSettingsComponent.prototype, "refreshOverlayRuntimeInfo").and.resolveTo();
        await TestBed.configureTestingModule({
            imports: [AdvancedSettingsComponent, NoopAnimationsModule, TranslateModule.forRoot()],
            providers: [
                { provide: ConfigService, useValue: {
                    config$: configs, system$: of(null), plugin_model_providers$: of([]), screens$: of([]),
                    changeConfig, getCurrentConfig: () => configs.value,
                } },
                { provide: CharacterService, useValue: { character$: of(null) } },
                { provide: ConfigBackupService, useValue: {} },
                { provide: TauriService, useValue: {
                    runOpenXrGazeIntegrationAction: gazeAction,
                    getOverlayRuntimeInfo: getRuntime, getOpenXrGazeIntegrationStatus: gazeAction,
                    overlayChanges$: overlayChanges,
                } },
                { provide: ChatService, useValue: {
                    get eventGrouping() { return chatGrouping; },
                    setEventGrouping: setChatGrouping,
                } },
                { provide: FontScaleService, useValue: { scale: 1 } },
                { provide: ApiKeyDetectionService, useValue: {} },
                { provide: PluginUIService, useValue: {} },
                { provide: MatDialog, useValue: {} },
                { provide: MatSnackBar, useValue: { open: notify } },
            ],
        }).compileComponents();
        fixture = TestBed.createComponent(AdvancedSettingsComponent);
        component = fixture.componentInstance;
        component.expandedPanels.overlay = true;
        fixture.detectChanges();
        await fixture.whenStable();
        fixture.detectChanges();
    });

    it("shows chat grouping defaults in a dedicated Advanced panel", () => {
        const panel = fixture.debugElement.query(By.css(".chat-settings-panel"));
        expect(panel.query(By.directive(MatSlideToggle)).injector.get(MatSlideToggle).checked).toBeFalse();
        const select = panel.query(By.directive(MatSelect)).injector.get(MatSelect);
        expect(select.disabled).toBeTrue();
        expect(select.value).toBe(2);
        expect(component.chatEventGroupingDelays).toEqual([1, 2, 3, 4, 5]);
    });

    it("changes only local display preferences when configuring chat grouping", async () => {
        const panel = fixture.debugElement.query(By.css(".chat-settings-panel"));
        panel.query(By.directive(MatSlideToggle)).triggerEventHandler("ngModelChange", true);
        await fixture.whenStable();
        fixture.detectChanges();
        expect(setChatGrouping).toHaveBeenCalledWith(true, 2);
        expect(panel.query(By.directive(MatSelect)).injector.get(MatSelect).disabled).toBeFalse();

        panel.query(By.directive(MatSelect)).triggerEventHandler("ngModelChange", 4);
        await fixture.whenStable();
        fixture.detectChanges();
        expect(setChatGrouping).toHaveBeenCalledWith(true, 4);
        expect(component.chatEventGrouping).toEqual({ enabled: true, delaySeconds: 4 });
        expect(changeConfig).not.toHaveBeenCalled();

        configs.next({ ...initialConfig, commander_name: "Updated preview" });
        fixture.detectChanges();
        expect(component.chatEventGrouping).toEqual({ enabled: true, delaySeconds: 4 });
    });

    it("localizes chat settings in French without changing their behavior", async () => {
        const translate = TestBed.inject(TranslateService);
        translate.setTranslation("fr", TRANSLATIONS["fr"]);
        translate.use("fr");
        await fixture.whenStable();
        fixture.detectChanges();
        const panel: HTMLElement = fixture.nativeElement.querySelector(".chat-settings-panel");
        expect(panel.textContent).toContain("Paramètres du chat");
        expect(panel.textContent).toContain("Regrouper les événements répétés");
        expect(panel.textContent).toContain("Délai d'inactivité");
        expect(component.chatEventGrouping).toEqual({ enabled: false, delaySeconds: 2 });
    });

    it("allows distance offsets from -1.5 to 1.5 metres in the placement slider", () => {
        const slider = fixture.debugElement.query(
            By.css(".vr-placement-grid .overlay-slider-field:last-child mat-slider"),
        ).injector.get(MatSlider);
        expect(slider.min).toBe(-1.5);
        expect(slider.max).toBe(1.5);
        expect(slider.step).toBe(0.01);
    });

    function releaseSlider(index = 0): void {
        fixture.debugElement.queryAll(By.directive(MatSliderThumb))[index]
            .triggerEventHandler("dragEnd");
    }

    it("updates the preview immediately but sends only the last value after 200 ms without input", fakeAsync(() => {
        const before = fixture.nativeElement.querySelector("app-vr-overlay-preview polygon").getAttribute("points");
        for (let i = 1; i <= 100; i++) {
            component.onVrConfigChange({ overlay_vr_horizontal_offset: i / 100 });
            tick(10);
        }
        fixture.detectChanges();
        expect(component.config?.overlay_vr_horizontal_offset).toBe(1);
        expect(fixture.nativeElement.querySelector("app-vr-overlay-preview polygon").getAttribute("points")).not.toBe(before);
        expect(changeConfig).not.toHaveBeenCalled();
        tick(189);
        expect(changeConfig).not.toHaveBeenCalled();
        tick(1);
        expect(changeConfig).toHaveBeenCalledOnceWith({ overlay_vr_horizontal_offset: 1 });
    }));

    it("batches multiple controls and flushes the final values on release without a trailing duplicate", fakeAsync(() => {
        component.onVrConfigChange({ overlay_vr_horizontal_offset: 0.1 });
        component.onVrConfigChange({ overlay_vr_vertical_offset: 0.2 });
        component.onVrConfigChange({ overlay_vr_horizontal_offset: 0.5 });
        releaseSlider();
        flushMicrotasks();
        expect(changeConfig).toHaveBeenCalledOnceWith({
            overlay_vr_horizontal_offset: 0.5, overlay_vr_vertical_offset: 0.2,
        });
        tick(500);
        releaseSlider();
        expect(changeConfig.calls.count()).toBe(1);
    }));

    it("wires every VR slider to local updates and a single release-time save", fakeAsync(() => {
        const values = ["0.5", "0.25", "0.1", "25", "-30", "1.2", "36"];
        const thumbs = fixture.debugElement.queryAll(By.directive(MatSliderThumb));
        expect(thumbs.length).toBe(7);
        thumbs.forEach((thumb, index) => {
            const input: HTMLInputElement = thumb.nativeElement;
            input.value = values[index];
            input.dispatchEvent(new Event("input"));
            fixture.detectChanges();
            expect(changeConfig.calls.count()).toBe(index);
            releaseSlider(index);
            flushMicrotasks();
            expect(changeConfig.calls.count()).toBe(index + 1);
        });
        expect(component.config?.overlay_vr_horizontal_offset).toBe(0.5);
        expect(component.config?.overlay_vr_vertical_offset).toBe(0.25);
        expect(component.config?.overlay_vr_distance_offset).toBe(0.1);
        expect(component.config?.overlay_vr_tilt_degrees).toBe(25);
        expect(component.config?.overlay_vr_yaw_degrees).toBe(-30);
        expect(component.config?.overlay_vr_size_meters).toBe(1.2);
        expect(component.config?.overlay_vr_curvature).toBe(0.1);
        tick(500);
        expect(changeConfig.calls.count()).toBe(7);
    }));

    it("requires world anchoring to enable adaptive sizing and batches its area factor", fakeAsync(() => {
        const section: HTMLElement = fixture.nativeElement.querySelector(".vr-adaptive-size");
        expect(section.textContent).toContain("vrInterface.requiresWorldAnchor");
        expect(section.querySelector("mat-slide-toggle")).toBeNull();
        expect(section.querySelector("input")).toBeNull();
        expect(section.querySelector("button")).toBeNull();
        component.onVrConfigChange({ overlay_vr_anchor: "world" });
        fixture.detectChanges();
        const toggle = fixture.debugElement.queryAll(By.directive(MatSlideToggle))
            .find(element => element.nativeElement.textContent.includes("vrInterface.gazeEnabled"));
        expect(toggle).toBeDefined();
        expect(section.textContent).not.toContain("vrInterface.requiresWorldAnchor");
        toggle!.triggerEventHandler("ngModelChange", true);
        component.onGazeReductionFactorChange(5);
        tick(200);
        expect(changeConfig).toHaveBeenCalledOnceWith({
            overlay_vr_anchor: "world", overlay_vr_gaze_zoom: true, overlay_vr_gaze_reduction_factor: 5,
        });
    }));

    it("rejects invalid area factors instead of saving them", fakeAsync(() => {
        for (const value of [null, 0, 0.5, NaN, Infinity]) {
            component.onGazeReductionFactorChange(value);
        }
        tick(300);
        expect(changeConfig).not.toHaveBeenCalled();
        expect(component.config?.overlay_vr_gaze_reduction_factor).toBe(5);
        component.onGazeReductionFactorChange(12.5);
        tick(200);
        expect(changeConfig).toHaveBeenCalledOnceWith({ overlay_vr_gaze_reduction_factor: 12.5 });
    }));

    it("updates the reduced-size settings frame immediately when editing the factor, before debounced saving", fakeAsync(() => {
        component.onVrConfigChange({ overlay_vr_anchor: "world", overlay_vr_gaze_zoom: true });
        fixture.detectChanges();
        const root: HTMLElement = fixture.nativeElement;
        const full = root.querySelector(".overlay-window")!.getAttribute("points");
        const before = root.querySelector(".reduced-window")!.getAttribute("points");
        fixture.debugElement.query(By.css(".vr-adaptive-size input"))
            .triggerEventHandler("ngModelChange", 9);
        fixture.detectChanges();
        expect(root.querySelector(".reduced-window")!.getAttribute("points")).not.toBe(before);
        expect(root.querySelector(".overlay-window")!.getAttribute("points")).toBe(full);
        expect(changeConfig).not.toHaveBeenCalled();
        tick(200);
        expect(changeConfig).toHaveBeenCalledOnceWith({
            overlay_vr_anchor: "world", overlay_vr_gaze_zoom: true, overlay_vr_gaze_reduction_factor: 9,
        });
    }));

    it("ignores stale slider echoes until the latest value is confirmed, without hiding unrelated updates", fakeAsync(() => {
        component.onVrConfigChange({ overlay_vr_horizontal_offset: 0.5 });
        tick(200);
        component.onVrConfigChange({ overlay_vr_horizontal_offset: 1 });
        configs.next({ ...initialConfig, overlay_vr_horizontal_offset: 0.5, commander_name: "Updated" });
        expect(component.config?.overlay_vr_horizontal_offset).toBe(1);
        expect(component.config?.commander_name).toBe("Updated");
        tick(200);
        configs.next({ ...initialConfig, overlay_vr_horizontal_offset: 0.5 });
        expect(component.config?.overlay_vr_horizontal_offset).toBe(1);
        configs.next({ ...initialConfig, overlay_vr_horizontal_offset: 1 });
        configs.next({ ...initialConfig, overlay_vr_horizontal_offset: -0.5 });
        expect(component.config?.overlay_vr_horizontal_offset).toBe(-0.5);
    }));

    it("does not acknowledge an unsent value when dragging back through an earlier position", fakeAsync(() => {
        component.onVrConfigChange({ overlay_vr_horizontal_offset: 0.5 });
        tick(200);
        component.onVrConfigChange({ overlay_vr_horizontal_offset: 0 });
        configs.next({ ...initialConfig });
        configs.next({ ...initialConfig, overlay_vr_horizontal_offset: 0.5 });
        expect(component.config?.overlay_vr_horizontal_offset).toBe(0);
        tick(200);
        expect(changeConfig.calls.mostRecent().args).toEqual([{ overlay_vr_horizontal_offset: 0 }]);
    }));

    it("reports save errors and restores the confirmed value rather than pretending it was saved", fakeAsync(() => {
        const error = new Error("Disconnected");
        const log = spyOn(console, "error");
        changeConfig.and.rejectWith(error);
        component.onVrConfigChange({ overlay_vr_horizontal_offset: 0.5 });
        tick(200);
        expect(log).toHaveBeenCalledWith("Error updating VR configuration:", error);
        expect(notify).toHaveBeenCalledWith("Error updating configuration", "OK", { duration: 5000 });
        expect(component.config?.overlay_vr_horizontal_offset).toBe(0);
    }));

    it("preserves a newer queued value if an older save fails", fakeAsync(() => {
        let rejectSave!: (error: Error) => void;
        changeConfig.and.returnValue(new Promise<void>((_resolve, reject) => rejectSave = reject));
        spyOn(console, "error");
        component.onVrConfigChange({ overlay_vr_horizontal_offset: 0.5 });
        tick(200);
        component.onVrConfigChange({ overlay_vr_horizontal_offset: 1 });
        rejectSave(new Error("Disconnected"));
        flushMicrotasks();
        expect(component.config?.overlay_vr_horizontal_offset).toBe(1);
        changeConfig.and.resolveTo();
        tick(200);
        expect(changeConfig.calls.mostRecent().args).toEqual([{ overlay_vr_horizontal_offset: 1 }]);
    }));

    it("flushes the final pending value when leaving settings and cancels its timer", fakeAsync(() => {
        component.onVrConfigChange({ overlay_vr_horizontal_offset: 0.5 });
        fixture.destroy();
        flushMicrotasks();
        expect(changeConfig).toHaveBeenCalledOnceWith({ overlay_vr_horizontal_offset: 0.5 });
        tick(500);
        expect(changeConfig.calls.count()).toBe(1);
    }));

    it("drops pending edits when the configuration is reset", fakeAsync(() => {
        component.onVrConfigChange({ overlay_vr_horizontal_offset: 0.5 });
        configs.next(null);
        tick(500);
        expect(component.config).toBeNull();
        expect(changeConfig).not.toHaveBeenCalled();
        configs.next({ ...initialConfig });
        expect(component.config?.overlay_vr_horizontal_offset).toBe(0);
    }));

    it("keeps non-VR settings immediate", fakeAsync(() => {
        void component.onConfigChange({ commander_name: "Updated" });
        flushMicrotasks();
        expect(changeConfig).toHaveBeenCalledOnceWith({ commander_name: "Updated" });
    }));

    it("updates OpenXR gaze integration without an unconditional restart notice", async () => {
        await component.runOpenXrGazeIntegrationAction("install");
        expect(gazeAction).toHaveBeenCalledOnceWith("install");
        expect(component.openXrGazeIntegration?.enabled).toBeTrue();
        expect(component.isGazeIntegrationBusy).toBeFalse();
        expect(notify).toHaveBeenCalledWith("vrInterface.gazeSetupDone", "OK", { duration: 10000 });
        expect(changeConfig).not.toHaveBeenCalled();
    });

    it("places the sliders directly after the preview and adaptive sizing after all placement controls", () => {
        const root: HTMLElement = fixture.nativeElement;
        const preview = root.querySelector("app-vr-overlay-preview")!;
        expect(preview.nextElementSibling?.querySelector("h4")?.textContent).toBe("Placement");
        expect(Array.from(root.querySelectorAll(".overlay-secondary-controls section h4"), heading => heading.textContent)).toEqual([
            "Placement", "Orientation", "Appearance", "vrInterface.gazeTitle",
        ]);
        expect(preview.querySelector("p")).toBeNull();
    });

    it("shows game restart text only when tracking actually changed for a running host", async () => {
        configs.next({ ...initialConfig, overlay_vr_anchor: "world" });
        component.overlayRuntimeInfo = runtimeInfo();
        component.openXrGazeIntegration = { supported: true, available: true, installed: true, enabled: true, restartRequired: false };
        fixture.detectChanges();
        expect(fixture.nativeElement.querySelector(".vr-game-restart")).toBeNull();
        gazeAction.and.resolveTo({ ...component.openXrGazeIntegration, restartRequired: true });
        await component.runOpenXrGazeIntegrationAction("install");
        fixture.detectChanges();
        expect(fixture.nativeElement.querySelector(".vr-game-restart")).not.toBeNull();
        expect(notify).toHaveBeenCalledWith("vrInterface.gazeGameRestart", "OK", { duration: 10000 });
        component.openXrGazeIntegration = { ...component.openXrGazeIntegration!, restartRequired: false };
        fixture.detectChanges();
        expect(fixture.nativeElement.querySelector(".vr-game-restart")).toBeNull();
    });

    it("compares UI edits with live settings, retains the notice on save, and clears it after overlay recreation", fakeAsync(() => {
        const config: Config = { ...initialConfig, overlay_vr_anchor: "world", overlay_vr_gaze_zoom: true };
        configs.next(config);
        component.overlayRuntimeInfo = runtimeInfo(config);
        expect(component.vrSettingsRestartRequired).toBeFalse();
        component.onGazeReductionFactorChange(9);
        expect(component.vrSettingsRestartRequired).toBeTrue();
        component.onGazeReductionFactorChange(5);
        expect(component.vrSettingsRestartRequired).toBeFalse();
        component.onGazeReductionFactorChange(9);
        tick(200);
        const updated = { ...config, overlay_vr_gaze_reduction_factor: 9 };
        configs.next(updated);
        expect(component.vrSettingsRestartRequired).toBeTrue();
        expect(getRuntime).not.toHaveBeenCalled();
        getRuntime.and.resolveTo(runtimeInfo(updated));
        (component.refreshOverlayRuntimeInfo as jasmine.Spy).and.callThrough();
        overlayChanges.next();
        flushMicrotasks();
        expect(component.vrSettingsRestartRequired).toBeFalse();
    }));

    it("refreshes actual runtime settings for already-applied voice factors and positioning instead of requiring a restart", fakeAsync(() => {
        const config: Config = { ...initialConfig, overlay_vr_anchor: "world", overlay_vr_gaze_zoom: true };
        configs.next(config);
        component.overlayRuntimeInfo = runtimeInfo(config);
        const updated = { ...config, overlay_vr_gaze_reduction_factor: 9, overlay_vr_vertical_offset: 0.2 };
        getRuntime.and.resolveTo(runtimeInfo(updated));
        (component.refreshOverlayRuntimeInfo as jasmine.Spy).and.callThrough();
        configs.next(updated);
        flushMicrotasks();
        expect(getRuntime).toHaveBeenCalledTimes(1);
        expect(component.vrSettingsRestartRequired).toBeFalse();
        expect(changeConfig).not.toHaveBeenCalled();
    }));

    it("does not request assistant restarts without an active overlay or for an unused factor", () => {
        component.overlayRuntimeInfo = { ...runtimeInfo(), activeVrSettings: null };
        component.onGazeReductionFactorChange(9);
        expect(component.vrSettingsRestartRequired).toBeFalse();
        component.overlayRuntimeInfo = runtimeInfo();
        expect(component.vrSettingsRestartRequired).toBeFalse();
    });

    it("links EDVR for OpenXR and hides OpenXR setup text and buttons for an OpenVR backend", () => {
        configs.next({ ...initialConfig, overlay_vr_anchor: "world", overlay_vr_gaze_zoom: true });
        component.overlayRuntimeInfo = runtimeInfo();
        component.openXrGazeIntegration = { supported: true, available: true, installed: true, enabled: true, restartRequired: false };
        fixture.detectChanges();
        const root: HTMLElement = fixture.nativeElement;
        expect(root.querySelector<HTMLAnchorElement>('a[href*="edvr-unofficial-patch"]')?.href)
            .toBe("https://github.com/characterecho-sean/edvr-unofficial-patch");
        expect(root.querySelector(".vr-adaptive-size button[mat-stroked-button]")).not.toBeNull();
        component.overlayRuntimeInfo = { ...runtimeInfo(), selectedBackend: "openvr" };
        fixture.detectChanges();
        expect(root.querySelector(".vr-adaptive-size button[mat-stroked-button]")).toBeNull();
        expect(root.textContent).not.toContain("vrInterface.gazeInstalled");
        expect(root.querySelector('a[href*="edvr-unofficial-patch"]')).toBeNull();
    });

    it("surfaces failed OpenXR gaze setup and clears its busy state", async () => {
        spyOn(console, "error");
        gazeAction.and.rejectWith(new Error("Registry denied"));
        await component.runOpenXrGazeIntegrationAction("install");
        expect(component.isGazeIntegrationBusy).toBeFalse();
        expect(notify).toHaveBeenCalledWith("vrInterface.gazeSetupFailed", "OK", { duration: 10000 });
    });

    it("shows the full-size fallback warning only when the live overlay reports unavailable tracking", () => {
        configs.next({ ...initialConfig, overlay_vr_anchor: "world", overlay_vr_gaze_zoom: true });
        component.overlayRuntimeInfo = { ...runtimeInfo(), gazeZoom: { status: "active" } };
        fixture.detectChanges();
        expect(fixture.nativeElement.querySelector(".vr-adaptive-size [role=alert]")).toBeNull();
        component.overlayRuntimeInfo = {
            ...runtimeInfo(), gazeZoom: { status: "unavailable", message: "Headset tracking is unavailable." },
        };
        fixture.detectChanges();
        expect(fixture.nativeElement.querySelector(".vr-adaptive-size [role=alert]").textContent)
            .toContain("vrInterface.gazeUnavailable");
        component.overlayRuntimeInfo = { ...runtimeInfo(), gazeZoom: { status: "active" } };
        fixture.detectChanges();
        expect(fixture.nativeElement.querySelector(".vr-adaptive-size [role=alert]")).toBeNull();
    });

    it("shows waiting as information and refreshes until OpenXR tracking becomes active", fakeAsync(() => {
        const config: Config = { ...initialConfig, overlay_vr_anchor: "world", overlay_vr_gaze_zoom: true };
        configs.next(config);
        getRuntime.and.returnValues(
            Promise.resolve({ ...runtimeInfo(config), gazeZoom: { status: "waiting", message: "Waiting for the game." } }),
            Promise.resolve({ ...runtimeInfo(config), gazeZoom: { status: "active" } }),
        );
        (component.refreshOverlayRuntimeInfo as jasmine.Spy).and.callThrough();
        void component.refreshOverlayRuntimeInfo(true);
        flushMicrotasks();
        fixture.detectChanges();
        expect(fixture.nativeElement.querySelector(".vr-tracking-waiting").getAttribute("role")).toBe("status");
        expect(fixture.nativeElement.querySelector(".vr-tracking-waiting").textContent).toContain("vrInterface.gazeWaiting");
        expect(fixture.nativeElement.querySelector(".vr-adaptive-size [role=alert]")).toBeNull();
        tick(1500);
        fixture.detectChanges();
        expect(component.overlayRuntimeInfo?.gazeZoom?.status).toBe("active");
        expect(fixture.nativeElement.querySelector(".vr-tracking-waiting")).toBeNull();
        tick(3000);
        expect(getRuntime).toHaveBeenCalledTimes(2);
        expect(notify).not.toHaveBeenCalled();
    }));
});
