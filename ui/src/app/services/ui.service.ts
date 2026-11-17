import {Injectable, NgZone, OnDestroy} from "@angular/core";
import {BaseMessage, TauriService} from "./tauri.service";
import {BehaviorSubject, Subject} from "rxjs";

export interface UISectionRequest {
    request_id: string;
    expanded: boolean;
    name?: string;
    index?: number;
}

export interface UIMessage extends BaseMessage {
    type: "ui",
    show?: string,
    submenu?: string,
    scroll?: "top" | "up" | "down" | "bottom",
    visible?: boolean,
    section?: UISectionRequest,
}

type UIChange = Pick<UIMessage, "type" | "show" | "submenu" | "scroll">;
type NavigationMessage = { type: "request-current" }
    | { type: "navigation"; navigation: UIChange }
    | { type: "visibility"; visible: boolean }
    | { type: "adjustment"; previousVisible: boolean };

@Injectable({
    providedIn: "root",
})
export class UIService implements OnDestroy {
    private changeUISubject = new BehaviorSubject<UIChange | null>(null);
    private currentNavigation: UIChange | null = null;
    public changeUI$ = this.changeUISubject.asObservable();
    private readonly sectionRequests = new Subject<UISectionRequest>();
    public readonly sectionRequests$ = this.sectionRequests.asObservable();
    private readonly overlayInterfaceVisibleSubject = new BehaviorSubject(true);
    private visibilityBeforeAdjustment: boolean | null = null;
    public readonly overlayInterfaceVisible$ = this.overlayInterfaceVisibleSubject.asObservable();
    private readonly vrAdjustmentActiveSubject = new BehaviorSubject(false);
    public readonly vrAdjustmentActive$ = this.vrAdjustmentActiveSubject.asObservable();
    private readonly navigation = new BroadcastChannel("covas-ui-navigation");

    private readonly subscription;

    constructor(private tauriService: TauriService, zone: NgZone) {
        this.subscription = tauriService.output$.subscribe((message) => {
            if (message.type === "vr_placement_state") {
                if (message["active"] === true) {
                    this.vrAdjustmentActiveSubject.next(true);
                    this.visibilityBeforeAdjustment ??= this.overlayInterfaceVisibleSubject.value;
                    this.overlayInterfaceVisibleSubject.next(true);
                } else if (message["active"] === false) {
                    this.vrAdjustmentActiveSubject.next(false);
                    if (this.visibilityBeforeAdjustment !== null) {
                        this.overlayInterfaceVisibleSubject.next(this.visibilityBeforeAdjustment);
                        this.visibilityBeforeAdjustment = null;
                    }
                }
            }
            if (message.type == 'ui') {
                const msg = message as UIMessage;
                if (msg.section) this.sectionRequests.next(msg.section);
                if (typeof msg.visible === "boolean" && this.visibilityBeforeAdjustment === null) {
                    this.overlayInterfaceVisibleSubject.next(msg.visible);
                }
                if (msg.show || msg.scroll) {
                    if (msg.show) this.currentNavigation = msg;
                    this.changeUISubject.next(msg);
                }
            }
        })
        this.navigation.onmessage = (event: MessageEvent<NavigationMessage>) => zone.run(() => {
            if (event.data.type === "request-current") {
                const navigation = this.currentNavigation;
                if (navigation?.show) {
                    this.navigation.postMessage({ type: "navigation", navigation } satisfies NavigationMessage);
                }
                if (this.visibilityBeforeAdjustment !== null) {
                    this.navigation.postMessage({
                        type: "adjustment", previousVisible: this.visibilityBeforeAdjustment,
                    } satisfies NavigationMessage);
                }
                this.navigation.postMessage({
                    type: "visibility", visible: this.overlayInterfaceVisibleSubject.value,
                } satisfies NavigationMessage);
            } else if (event.data.type === "navigation") {
                this.currentNavigation = event.data.navigation;
                this.changeUISubject.next(event.data.navigation);
            } else if (event.data.type === "visibility") {
                if (this.visibilityBeforeAdjustment === null) this.overlayInterfaceVisibleSubject.next(event.data.visible);
            } else if (event.data.type === "adjustment") {
                this.vrAdjustmentActiveSubject.next(true);
                this.visibilityBeforeAdjustment = event.data.previousVisible;
                this.overlayInterfaceVisibleSubject.next(true);
            }
        });
        this.navigation.postMessage({ type: "request-current" } satisfies NavigationMessage);
    }

    public showTab(tabName: string, submenu?: string) {
        const navigation: UIChange = { type: "ui", show: tabName, submenu };
        this.currentNavigation = navigation;
        this.changeUISubject.next(navigation);
        this.navigation.postMessage({ type: "navigation", navigation } satisfies NavigationMessage);
    }

    ngOnDestroy(): void {
        this.subscription.unsubscribe();
        this.navigation.close();
    }
}
