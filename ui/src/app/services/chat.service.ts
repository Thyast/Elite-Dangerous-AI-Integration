import { Injectable, OnDestroy } from "@angular/core";
import { BehaviorSubject, filter, Subscription } from "rxjs";
import { BaseMessage, TauriService } from "./tauri.service";
import { UIService } from "./ui.service";

export interface ChatMessage extends BaseMessage {
    type: "chat";
    role: string;
    message: string;
    processingText?: string;
    synthetic?: boolean;
    tool_call_id?: string;
    actor_id?: string;
    actor_name?: string;
    avatar_id?: string;
    avatar_url?: string;
    display_name?: string;
    display_color?: string;
    plugin_event_name?: string;
}

export interface DisplayChatMessage extends ChatMessage {
    repeatCount?: number;
}

export interface ChatEventGroupingOptions {
    enabled: boolean;
    delaySeconds: number;
}

export const CHAT_EVENT_GROUPING_DELAYS: readonly number[] = [1, 2, 3, 4, 5];
const EVENT_GROUPING_ENABLED_KEY = "covas.ui.chat-event-grouping.enabled";
const EVENT_GROUPING_DELAY_KEY = "covas.ui.chat-event-grouping.delay";
const EVENT_GROUPING_MAX_WAIT_MS = 5000;

interface PendingChatEvent {
    message: ChatMessage;
    count: number;
    startedAt: number;
    timer?: ReturnType<typeof setTimeout>;
}

export interface ToolEvent {
    kind: 'tool';
    request: any[];
    results: any[];
    text?: string[];
}

export interface ToolProcessingEvent {
    kind: 'tool_processing';
    tool_call_id: string;
    name: string;
    content: any;
    text?: string;
}

export interface EventMessage extends BaseMessage {
    type: 'event';
    event: ToolEvent | ToolProcessingEvent;
}

@Injectable({
    providedIn: "root",
})
export class ChatService implements OnDestroy {
    private chatHistorySubject = new BehaviorSubject<ChatMessage[]>([]);
    public chatHistory$ = this.chatHistorySubject.asObservable();

    private displayChatHistorySubject = new BehaviorSubject<DisplayChatMessage[]>([]);
    public displayChatHistory$ = this.displayChatHistorySubject.asObservable();

    private chatMessageSubject = new BehaviorSubject<ChatMessage | null>(null);
    public chatMessage$ = this.chatMessageSubject.asObservable()

    private searchResultSubject = new BehaviorSubject<any | null>(null);
    public searchResult$ = this.searchResultSubject.asObservable();

    private readonly activeToolActionMessages = new Map<string, ChatMessage>();
    private readonly completedSyntheticActionMessages = new Set<string>();
    private readonly pendingEvents = new Map<string, PendingChatEvent>();
    private readonly subscriptions = new Subscription();
    private eventGroupingOptions: ChatEventGroupingOptions = {
        enabled: localStorage.getItem(EVENT_GROUPING_ENABLED_KEY) === "true",
        delaySeconds: this.readEventGroupingDelay(),
    };

    constructor(private tauriService: TauriService, private uiService: UIService) {
        // Subscribe to log messages from the TauriService
        this.subscriptions.add(this.tauriService.output$.pipe(
            filter((message): message is ChatMessage =>
                message.type === "chat"
            ),
        ).subscribe((chatMessage) => {
            if (chatMessage.type === "chat") {
                if (this.shouldSuppressCompletedSyntheticAction(chatMessage)) {
                    return;
                }
                this.chatMessageSubject.next(chatMessage);
                const currentLogs = this.chatHistorySubject.getValue();
                this.chatHistorySubject.next([...currentLogs, chatMessage]);
                this.addDisplayMessage(chatMessage);
            }
        }));

        this.subscriptions.add(this.tauriService.output$.pipe(
            filter((message): message is EventMessage =>
                message.type === "event" && ["tool", "tool_processing"].includes((message as any).event?.kind)
            ),
        ).subscribe((eventMessage) => {
            if (eventMessage.event.kind === "tool_processing") {
                this.handleToolProcessingEvent(eventMessage, eventMessage.event);
                return;
            }

            const toolEvent = eventMessage.event;
            this.handleFinalToolEvent(toolEvent);
            const webSearchRequestIndex = toolEvent.request.findIndex((r: any) => r.function?.name === 'web_search_agent');
            
            if (webSearchRequestIndex !== -1 && toolEvent.results[webSearchRequestIndex]) {
                this.searchResultSubject.next(toolEvent.results[webSearchRequestIndex]);
                this.uiService.showTab('search');
            }
        }));
    }

    public get eventGrouping(): Readonly<ChatEventGroupingOptions> {
        return this.eventGroupingOptions;
    }

    public setEventGrouping(enabled: boolean, delaySeconds: number): void {
        if (!CHAT_EVENT_GROUPING_DELAYS.includes(delaySeconds)) {
            throw new RangeError("Event grouping delay must be between 1 and 5 seconds.");
        }
        localStorage.setItem(EVENT_GROUPING_ENABLED_KEY, String(enabled));
        localStorage.setItem(EVENT_GROUPING_DELAY_KEY, String(delaySeconds));
        if (enabled === this.eventGroupingOptions.enabled && delaySeconds === this.eventGroupingOptions.delaySeconds) {
            return;
        }
        for (const name of [...this.pendingEvents.keys()]) {
            this.flushPendingEvent(name);
        }
        this.eventGroupingOptions = { enabled, delaySeconds };
    }

    public ngOnDestroy(): void {
        this.subscriptions.unsubscribe();
        this.cancelPendingEvents();
    }

    public clearChat(): void {
        this.cancelPendingEvents();
        this.chatHistorySubject.next([]);
        this.displayChatHistorySubject.next([]);
        this.activeToolActionMessages.clear();
        this.completedSyntheticActionMessages.clear();
    }

    public getCurrentChat(): ChatMessage[] {
        return this.chatHistorySubject.getValue();
    }

    private readEventGroupingDelay(): number {
        const stored = localStorage.getItem(EVENT_GROUPING_DELAY_KEY);
        if (stored === null) {
            return 2;
        }
        const delay = Number(stored);
        if (!CHAT_EVENT_GROUPING_DELAYS.includes(delay)) {
            console.warn("Invalid stored event grouping delay; using 2 seconds.", stored);
            return 2;
        }
        return delay;
    }

    private addDisplayMessage(message: ChatMessage): void {
        if (!this.eventGroupingOptions.enabled || message.role.toLowerCase() !== "event") {
            this.displayChatHistorySubject.next([...this.displayChatHistorySubject.value, message]);
            return;
        }

        const name = message.message.trim().toLowerCase();
        const now = Date.now();
        const pending: PendingChatEvent = this.pendingEvents.get(name) ?? {
            message,
            count: 0,
            startedAt: now,
        };
        clearTimeout(pending.timer);
        pending.count++;
        const wait = Math.max(0, Math.min(
            this.eventGroupingOptions.delaySeconds * 1000,
            pending.startedAt + EVENT_GROUPING_MAX_WAIT_MS - now,
        ));
        pending.timer = setTimeout(() => this.flushPendingEvent(name), wait);
        this.pendingEvents.set(name, pending);
    }

    private flushPendingEvent(name: string): void {
        const pending = this.pendingEvents.get(name);
        if (!pending) {
            return;
        }
        clearTimeout(pending.timer);
        this.pendingEvents.delete(name);
        this.displayChatHistorySubject.next([
            ...this.displayChatHistorySubject.value,
            { ...pending.message, repeatCount: pending.count },
        ]);
    }

    private cancelPendingEvents(): void {
        for (const pending of this.pendingEvents.values()) {
            clearTimeout(pending.timer);
        }
        this.pendingEvents.clear();
    }

    private updateDisplayMessage(existing: ChatMessage, updated: ChatMessage): void {
        this.displayChatHistorySubject.next(
            this.displayChatHistorySubject.value.map((item) => item === existing ? updated : item),
        );
    }

    private handleToolProcessingEvent(eventMessage: EventMessage, event: ToolProcessingEvent): void {
        if (!['web_search_agent', 'generate_overlay_ui'].includes(event.name)) {
            return;
        }

        const content = event.content ?? {};
        const message = this.formatProcessingMessage(event.name, content);
        const processingText = this.formatToolProcessingText(content);
        const currentLogs = this.chatHistorySubject.getValue();
        const existing = this.activeToolActionMessages.get(event.tool_call_id);

        if (existing) {
            const updated: ChatMessage = {
                ...existing,
                message,
                processingText: processingText ?? existing.processingText,
            };
            this.activeToolActionMessages.set(event.tool_call_id, updated);
            this.chatHistorySubject.next(currentLogs.map((item) => item === existing ? updated : item));
            this.updateDisplayMessage(existing, updated);
            return;
        }

        const syntheticMessage: ChatMessage = {
            type: 'chat',
            timestamp: eventMessage.timestamp,
            index: eventMessage.index,
            role: 'action',
            message,
            processingText,
            synthetic: true,
            tool_call_id: event.tool_call_id,
        };

        this.activeToolActionMessages.set(event.tool_call_id, syntheticMessage);
        this.chatMessageSubject.next(syntheticMessage);
        this.chatHistorySubject.next([...currentLogs, syntheticMessage]);
        this.addDisplayMessage(syntheticMessage);
    }

    private handleFinalToolEvent(event: ToolEvent): void {
        for (const result of event.results ?? []) {
            const toolCallId = result?.tool_call_id;
            if (!toolCallId) {
                continue;
            }
            const existing = this.activeToolActionMessages.get(toolCallId);
            if (!existing) {
                continue;
            }
            const updated: ChatMessage = { ...existing, processingText: undefined };
            this.activeToolActionMessages.delete(toolCallId);
            this.completedSyntheticActionMessages.add(existing.message);
            this.chatHistorySubject.next(this.chatHistorySubject.getValue().map((item) => item === existing ? updated : item));
            this.updateDisplayMessage(existing, updated);
        }
    }

    private formatToolProcessingText(content: any): string | undefined {
        if (typeof content?.internal_tool_name !== 'string') {
            return undefined;
        }
        const toolName = content.internal_tool_name;
        const status = typeof content?.status === 'string'
            ? content.status
            : 'processing';
        return `${toolName} ${status}`;
    }

    private formatProcessingMessage(actionName: string, content: any): string {
        if (actionName === 'generate_overlay_ui') {
            const instruction = typeof content?.instruction === 'string' && content.instruction.trim()
                ? content.instruction.trim()
                : 'overlay UI';
            return `Generating UI: ${instruction}`;
        }

        const query = typeof content?.query === 'string' && content.query.trim()
            ? content.query.trim()
            : 'web search';
        return `Searching: ${query}`;
    }

    private shouldSuppressCompletedSyntheticAction(chatMessage: ChatMessage): boolean {
        if (chatMessage.role !== 'action' || !this.completedSyntheticActionMessages.has(chatMessage.message)) {
            return false;
        }
        this.completedSyntheticActionMessages.delete(chatMessage.message);
        return true;
    }
}
