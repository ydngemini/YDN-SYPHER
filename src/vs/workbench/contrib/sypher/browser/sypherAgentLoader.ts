/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { DisposableStore, Disposable } from '../../../../base/common/lifecycle.js';
import { URI } from '../../../../base/common/uri.js';
import { Emitter, Event } from '../../../../base/common/event.js';
import { localize, localize2 } from '../../../../nls.js';
import { IFileService } from '../../../../platform/files/common/files.js';
import { ILogService } from '../../../../platform/log/common/log.js';
import { INotificationService } from '../../../../platform/notification/common/notification.js';
import { IStorageService, StorageScope, StorageTarget } from '../../../../platform/storage/common/storage.js';
import { IWorkspaceContextService } from '../../../../platform/workspace/common/workspace.js';
import { createDecorator, ServicesAccessor } from '../../../../platform/instantiation/common/instantiation.js';
import { Action2, MenuId } from '../../../../platform/actions/common/actions.js';
import { IQuickInputService, IQuickPickItem } from '../../../../platform/quickinput/common/quickInput.js';
import { IWorkbenchContribution } from '../../../common/contributions.js';

const AGENTS_DIR = '.sypher/agents';
const ACTIVE_AGENT_STORAGE_KEY = 'sypher.activeAgentId';

export interface IAgentPersona extends IQuickPickItem {
	readonly id: string;
	readonly uri: URI;
}

export const ISypherAgentService = createDecorator<ISypherAgentService>('sypherAgentService');

export interface ISypherAgentService {
	readonly _serviceBrand: undefined;
	readonly agents: readonly IAgentPersona[];
	readonly activeAgent: IAgentPersona | undefined;
	readonly onDidChangeAgents: Event<readonly IAgentPersona[]>;
	readonly onDidChangeActiveAgent: Event<IAgentPersona | undefined>;
	setActiveAgent(agent: IAgentPersona): void;
}

export class SypherAgentService extends Disposable implements ISypherAgentService {

	declare readonly _serviceBrand: undefined;

	private _agents: IAgentPersona[] = [];
	private _activeAgent: IAgentPersona | undefined;

	private readonly _onDidChangeAgents = this._register(new Emitter<readonly IAgentPersona[]>());
	readonly onDidChangeAgents = this._onDidChangeAgents.event;

	private readonly _onDidChangeActiveAgent = this._register(new Emitter<IAgentPersona | undefined>());
	readonly onDidChangeActiveAgent = this._onDidChangeActiveAgent.event;

	get agents(): readonly IAgentPersona[] { return this._agents; }
	get activeAgent(): IAgentPersona | undefined { return this._activeAgent; }

	constructor(
		@IWorkspaceContextService private readonly workspaceContextService: IWorkspaceContextService,
		@IFileService private readonly fileService: IFileService,
		@IStorageService private readonly storageService: IStorageService,
		@ILogService private readonly logService: ILogService,
	) {
		super();
		this.initialize();
	}

	private async initialize(): Promise<void> {
		const folders = this.workspaceContextService.getWorkspace().folders;
		if (folders.length === 0) {
			return;
		}

		const agentsDir = URI.joinPath(folders[0].uri, ...AGENTS_DIR.split('/'));

		if (!await this.fileService.exists(agentsDir)) {
			await this.fileService.createFolder(agentsDir);
			this.logService.info('[SypherAgents] Created .sypher/agents/');
		}

		await this.refreshAgents(agentsDir);

		const storedId = this.storageService.get(ACTIVE_AGENT_STORAGE_KEY, StorageScope.WORKSPACE);
		if (storedId) {
			const found = this._agents.find(a => a.id === storedId);
			if (found) {
				this._activeAgent = found;
			}
		}

		const watcher = this._register(this.fileService.createWatcher(agentsDir, { recursive: false, excludes: [] }));
		this._register(watcher.onDidChange(() => this.refreshAgents(agentsDir)));
	}

	private async refreshAgents(agentsDir: URI): Promise<void> {
		try {
			const stat = await this.fileService.resolve(agentsDir);
			const mdFiles = (stat.children ?? []).filter(c => !c.isDirectory && c.name.endsWith('.md'));
			this._agents = await Promise.all(mdFiles.map(f => this.parsePersona(f.resource)));
			if (this._activeAgent) {
				this._activeAgent = this._agents.find(a => a.id === this._activeAgent!.id);
			}
			this._onDidChangeAgents.fire(this._agents);
		} catch (err) {
			this.logService.warn(`[SypherAgents] Failed to scan .sypher/agents/: ${err}`);
		}
	}

	private async parsePersona(uri: URI): Promise<IAgentPersona> {
		const filename = uri.path.split('/').pop() ?? '';
		const id = filename.replace(/\.md$/, '');
		const label = id.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
		let description = '';
		try {
			const content = await this.fileService.readFile(uri);
			const text = content.value.toString();
			const descLine = text.split('\n').map(l => l.trim()).filter(l => l && !l.startsWith('#'))[0];
			description = descLine ?? '';
		} catch {
			// non-fatal: leave description empty
		}
		return { id, label, description, uri };
	}

	setActiveAgent(agent: IAgentPersona): void {
		this._activeAgent = agent;
		this.storageService.store(ACTIVE_AGENT_STORAGE_KEY, agent.id, StorageScope.WORKSPACE, StorageTarget.MACHINE);
		this._onDidChangeActiveAgent.fire(agent);
		this.logService.info(`[SypherAgents] Active agent: ${agent.label}`);
	}
}

/**
 * Workbench contribution whose sole role is to trigger SypherAgentService
 * instantiation at the AfterRestored lifecycle phase.
 */
export class SypherAgentLoader implements IWorkbenchContribution {

	static readonly ID = 'workbench.contrib.sypherAgentLoader';

	constructor(
		@ISypherAgentService _agentService: ISypherAgentService,
	) { }
}

export class LoadSypherAgentAction extends Action2 {

	static readonly ID = 'sypher.loadAgent';

	constructor() {
		super({
			id: LoadSypherAgentAction.ID,
			title: localize2('sypher.loadAgent', "SYPHER: Load Agent Persona"),
			menu: { id: MenuId.CommandPalette },
		});
	}

	override async run(accessor: ServicesAccessor): Promise<void> {
		const agentService = accessor.get(ISypherAgentService);
		const quickInputService = accessor.get(IQuickInputService);
		const notificationService = accessor.get(INotificationService);

		if (agentService.agents.length === 0) {
			notificationService.info(
				localize('sypher.noAgents', "No agent personas found in .sypher/agents/ — add a .md file to get started.")
			);
			return;
		}

		const disposables = new DisposableStore();
		const qp = disposables.add(quickInputService.createQuickPick<IAgentPersona>());
		qp.placeholder = localize('sypher.pickAgent', "Select a SYPHER agent persona to activate");
		qp.matchOnDescription = true;
		qp.items = [...agentService.agents];

		if (agentService.activeAgent) {
			const active = qp.items.filter(i => (i as IAgentPersona).id === agentService.activeAgent!.id);
			qp.activeItems = active;
		}

		disposables.add(qp.onDidHide(() => disposables.dispose()));
		disposables.add(qp.onDidAccept(() => {
			const selected = qp.activeItems[0] as IAgentPersona | undefined;
			if (selected) {
				agentService.setActiveAgent(selected);
				qp.hide();
			}
		}));

		qp.show();
	}
}
