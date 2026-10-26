<script lang="ts">
	import { onMount } from 'svelte';
	import AppShell from '$lib/components/AppShell.svelte';
	import Alert from '$lib/components/ui/Alert.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import EmptyState from '$lib/components/ui/EmptyState.svelte';
	import Input from '$lib/components/ui/Input.svelte';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import Panel from '$lib/components/ui/Panel.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';
	import { getBackendHttpBase, machineHttpBaseUrlFromWsUrl } from '$lib/backend';
	import { getMachinesContext } from '$lib/machines/context';
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import Search from '@lucide/svelte/icons/search';

	type Segment = { id: string; run_id: string | null; started_at: number; ended_at: number | null; frames: number; bytes: number; dropped: number; status: string };
	type Frame = { seq: number; captured_at: number };
	type Event = { id: number; kind: string; at: number; piece_uuid: string | null; data: Record<string, unknown> };
	type Request = { id: string; label: string | null; started_at: number; ended_at: number; status_code: number | null; error: string | null; evicted_at: number | null; images: Array<{ path: string; captured_at: number | null; channel: number | null; source: string | null }>; response: unknown };
	type Trace = {
		summary: Record<string, unknown> | null;
		events: Event[];
		requests: Request[];
		images: Array<{ id: number; ts: number | null; source: string | null; available_locally: boolean }>;
		predictions: Array<{ request: string; provider: string; answers: Array<{ kind: string; value_id: string; score: number | null; applied: boolean }> }>;
		incidents: Array<{ id: number; kind: string; severity: string | null; triggered_at: number; reason: string | null }>;
		chute_driver_telemetry: Array<Record<string, unknown>>;
		video_segment: Segment | null;
		physical_position: null;
	};

	const manager = getMachinesContext();
	const base = () => machineHttpBaseUrlFromWsUrl(manager.selectedMachine?.url) ?? getBackendHttpBase();
	let activeBase = $state(base());
	let status = $state<Record<string, unknown> | null>(null);
	let segments = $state<Segment[]>([]);
	let events = $state<Event[]>([]);
	let trace = $state<Trace | null>(null);
	let selectedSegment = $state<Segment | null>(null);
	let frames = $state<Frame[]>([]);
	let frameIndex = $state(0);
	let playing = $state(false);
	let pieceQuery = $state('');
	let runQuery = $state('');
	let kindQuery = $state('');
	let searchPart = $state('');
	let searchColor = $state('');
	let searchCategory = $state('');
	let searchBin = $state('');
	let searchIncident = $state('');
	let searchRefused = $state(false);
	let searchStall = $state(false);
	let searchClassificationError = $state(false);
	let searchRecordingGap = $state(false);
	let matchingPieces = $state<Array<{ uuid: string; part_id: string | null; color_id: string | null; category_id: string | null; seen_at: number | null }>>([]);
	let error = $state<string | null>(null);
	let loading = $state(false);
	let frameLoading = $state(false);
	let playTimer: ReturnType<typeof setInterval> | null = null;
	const currentFrame = $derived(frames[frameIndex] ?? null);
	const frameUrl = $derived(currentFrame && selectedSegment
		? `${activeBase}/api/machine-investigation/segments/${selectedSegment.id}/frames/${currentFrame.seq}`
		: null);

	function when(ts: number | null | undefined): string {
		return ts == null ? 'Unknown time' : new Date(ts * 1000).toLocaleString();
	}
	function bytes(n: number): string {
		return n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(1)} GB` : `${(n / 1024 ** 2).toFixed(1)} MB`;
	}
	async function jsonGet(path: string): Promise<any> {
		const response = await fetch(`${activeBase}${path}`);
		if (!response.ok) throw new Error(`HTTP ${response.status} loading ${path}`);
		return response.json();
	}

	async function refresh() {
		try {
			const [health, videos] = await Promise.all([
				jsonGet('/api/machine-investigation/status'),
				jsonGet('/api/machine-investigation/segments')
			]);
			status = health;
			segments = videos.segments ?? [];
			if (!selectedSegment && segments.length > 0) await chooseSegment(segments[0]);
			error = null;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not load machine evidence';
		}
	}

	async function loadEvents() {
		try {
			const params = new URLSearchParams({ limit: '200' });
			if (pieceQuery.trim()) params.set('piece_uuid', pieceQuery.trim());
			if (runQuery.trim()) params.set('run_id', runQuery.trim());
			if (kindQuery.trim()) params.set('kind', kindQuery.trim());
			events = (await jsonGet(`/api/machine-investigation/events?${params}`)).events ?? [];
			error = null;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not load events';
		}
	}

	async function searchPieces() {
		try {
			const params = new URLSearchParams();
			for (const [key, value] of [['part_id', searchPart], ['color_id', searchColor], ['category_id', searchCategory], ['bin', searchBin], ['incident_kind', searchIncident], ['run_id', runQuery]]) {
				if (value.trim()) params.set(key, value.trim());
			}
			if (searchRefused) params.set('refused_motor_command', 'true');
			if (searchStall) params.set('stall', 'true');
			if (searchClassificationError) params.set('classification_error', 'true');
			if (searchRecordingGap) params.set('recording_gap', 'true');
			matchingPieces = (await jsonGet(`/api/machine-investigation/search?${params}`)).pieces ?? [];
			error = null;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not search pieces';
		}
	}

	async function chooseSegment(segment: Segment, at?: number) {
		stopPlayback();
		selectedSegment = segment;
		frameLoading = true;
		try {
			const center = at ?? segment.started_at + 15;
			const payload = await jsonGet(`/api/machine-investigation/segments/${segment.id}/frames?at=${center}&span=30`);
			frames = payload.frames ?? [];
			const next = frames.findIndex((frame) => frame.captured_at >= center);
			frameIndex = next < 0 ? Math.max(0, frames.length - 1) : next;
		} catch (e) {
			frames = [];
			error = e instanceof Error ? e.message : 'Could not load video frames';
		} finally {
			frameLoading = false;
		}
	}

	async function jumpTo(at: number) {
		try {
			const payload = await jsonGet(`/api/machine-investigation/seek?at=${at}`);
			if (!payload.segment) {
				error = 'No recording is available at that time.';
				return;
			}
			await chooseSegment(payload.segment, at);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not seek video';
		}
	}

	async function openPiece(uuid: string) {
		loading = true;
		pieceQuery = uuid;
		try {
			trace = await jsonGet(`/api/machine-investigation/pieces/${encodeURIComponent(uuid)}`);
			history.replaceState(null, '', `${location.pathname}?piece=${encodeURIComponent(uuid)}`);
			if (trace?.video_segment) {
				const seen = Number(trace.summary?.seen_at ?? trace.video_segment.started_at);
				await chooseSegment(trace.video_segment, seen);
			}
			error = null;
		} catch (e) {
			trace = null;
			error = e instanceof Error ? e.message : 'Could not load piece evidence';
		} finally {
			loading = false;
		}
	}

	function stopPlayback() {
		playing = false;
		if (playTimer) clearInterval(playTimer);
		playTimer = null;
	}
	function startPlayback() {
		if (!currentFrame || frameIndex >= frames.length - 1) return;
		stopPlayback();
		playing = true;
		const start = performance.now();
		const initial = currentFrame.captured_at;
		playTimer = setInterval(() => {
			const target = initial + (performance.now() - start) / 1000;
			let next = frameIndex;
			while (next + 1 < frames.length && frames[next + 1].captured_at <= target) next++;
			frameIndex = next;
			if (next >= frames.length - 1) stopPlayback();
		}, 50);
	}

	onMount(() => {
		const requested = new URLSearchParams(location.search).get('piece');
		void (async () => {
			await refresh();
			await loadEvents();
			if (requested) await openPiece(requested);
		})();
		const timer = setInterval(() => { void refresh(); }, 10_000);
		return () => { clearInterval(timer); stopPlayback(); };
	});
	$effect(() => {
		const next = base();
		if (next === activeBase) return;
		activeBase = next;
		status = null;
		segments = [];
		selectedSegment = null;
		frames = [];
		trace = null;
		void refresh();
		void loadEvents();
	});
</script>

<svelte:head><title>Machine investigation - Sorter</title></svelte:head>

<AppShell>
	<div class="mx-auto flex w-full max-w-[1500px] flex-col gap-(--gap-panels) px-4 py-6 sm:px-6">
		<PageHeader title="Machine investigation" description="Review recorded camera frames, classification evidence, and motor commands for each piece." />
		{#if error}<Alert tone="danger">{error}</Alert>{/if}
		<Alert tone="info">A recorded destination and a controller acknowledgment do not confirm where a piece physically landed. This machine has no independent chute angle sensor.</Alert>
		<Panel title="Recording health" description="Classification camera, while sorting is running">
			{#if status}
				<div class="grid gap-3 text-sm sm:grid-cols-3">
					<div><strong>{status.recording ? 'Recording' : 'Not recording'}</strong><div class="text-ink-muted">Last frame: {when(status.last_frame_at as number | null)}</div></div>
					<div><strong>{bytes(Number(status.bytes ?? 0))} video stored</strong><div class="text-ink-muted">Video limit: {bytes(Number(status.max_bytes ?? 0))} · {status.segments} segments</div><div class="text-ink-muted">Request evidence: {bytes(Number(status.request_bytes ?? 0))} / {bytes(Number(status.request_max_bytes ?? 0))}</div></div>
					<div><strong>{status.dropped} dropped frames</strong><div class="text-ink-muted">{status.last_error ?? status.request_last_error ?? 'No recorder error reported'}</div></div>
				</div>
			{:else}<Spinner />{/if}
		</Panel>
		<div class="grid gap-(--gap-panels) lg:grid-cols-[minmax(0,2fr)_minmax(290px,1fr)]">
			<Panel title="Classification camera recording" description="Choose a segment or jump here from a piece event.">
				<div class="mb-3 flex flex-wrap gap-2">
					{#each segments as segment (segment.id)}
						<Button size="sm" variant={selectedSegment?.id === segment.id ? 'primary' : 'secondary'} onclick={() => chooseSegment(segment)}>{when(segment.started_at)} · {segment.status}</Button>
					{/each}
				</div>
				{#if frameLoading}<Spinner />
				{:else if frameUrl}
					<div class="overflow-hidden rounded-item bg-well"><img src={frameUrl} alt="Recorded classification camera frame" class="mx-auto max-h-[65vh] w-auto max-w-full" /></div>
					<div class="mt-3 flex items-center gap-3">
						<Button icon={playing ? Pause : Play} label={playing ? 'Pause recording' : 'Play recording'} onclick={playing ? stopPlayback : startPlayback} />
						<input type="range" min="0" max={Math.max(0, frames.length - 1)} step="1" bind:value={frameIndex} oninput={stopPlayback} aria-label="Recording position" class="min-w-0 flex-1" />
						<span class="num text-sm text-ink-muted">{when(currentFrame?.captured_at)}</span>
					</div>
					<p class="mt-2 text-sm text-ink-muted">{frames.length} indexed frames in this 30-second window. Gaps are shown in the event log.</p>
				{:else}<EmptyState title="No recorded frames in this window">Select another segment or seek from an event.</EmptyState>{/if}
			</Panel>
			<Panel title="Find a piece" description="Open the evidence for one piece UUID, or search recent recorded pieces.">
				<div class="flex gap-2"><Input bind:value={pieceQuery} placeholder="Piece UUID" /><Button icon={Search} label="Open piece" loading={loading} onclick={() => openPiece(pieceQuery.trim())} /></div>
				<div class="mt-4 grid grid-cols-2 gap-2">
					<Input bind:value={searchPart} placeholder="Part ID" />
					<Input bind:value={searchColor} placeholder="Color ID" />
					<Input bind:value={searchCategory} placeholder="Category" />
					<Input bind:value={searchBin} placeholder="Bin: layer,section,bin" />
					<Input bind:value={searchIncident} placeholder="Incident kind" />
				</div>
				<div class="mt-3 flex flex-wrap gap-3 text-sm">
					<label><input type="checkbox" bind:checked={searchRefused} /> Refused motor command</label>
					<label><input type="checkbox" bind:checked={searchStall} /> Stall</label>
					<label><input type="checkbox" bind:checked={searchClassificationError} /> Classification error</label>
					<label><input type="checkbox" bind:checked={searchRecordingGap} /> Recording gap</label>
				</div>
				<div class="mt-2"><Button onclick={searchPieces}>Search pieces</Button></div>
				{#if matchingPieces.length > 0}
					<div class="mt-3 max-h-80 overflow-y-auto divide-y divide-line">
						{#each matchingPieces as piece (piece.uuid)}
							<button type="button" class="w-full py-2 text-left text-sm hover:bg-hover" onclick={() => openPiece(piece.uuid)}><strong>{piece.part_id ?? 'Unknown'}</strong> · {piece.color_id ?? '?'} · {piece.category_id ?? 'No category'}<span class="block text-xs text-ink-muted">{when(piece.seen_at)} · {piece.uuid}</span></button>
						{/each}
					</div>
				{/if}
			</Panel>
		</div>
		{#if trace}
			<Panel title="Piece trace" description={String(trace.summary?.uuid ?? pieceQuery)}>
				<div class="grid gap-4 lg:grid-cols-2">
					<div>
						<p class="text-sm">Part <strong>{trace.summary?.part_id ?? 'Unknown'}</strong> · color <strong>{trace.summary?.color_id ?? 'Unknown'}</strong> · category <strong>{trace.summary?.category_id ?? 'Unknown'}</strong></p>
						<p class="mt-1 text-sm text-ink-muted">Recorded bin: {trace.summary?.bin ? JSON.stringify(trace.summary.bin) : 'None'} · physical landing unconfirmed</p>
						{#if trace.summary?.part_correct != null || trace.summary?.color_corrected_id}
							<p class="mt-2 text-sm">Later operator correction: part {trace.summary?.part_correct === true ? 'confirmed' : trace.summary?.part_correct === false ? 'marked wrong' : 'not reviewed'} · corrected color {trace.summary?.color_corrected_id ?? 'none'}.</p>
						{/if}
						<h3 class="mt-5 font-semibold">Timeline</h3>
						<div class="mt-2 max-h-96 overflow-y-auto divide-y divide-line">
							{#each [...trace.events].reverse() as event (event.id)}
								<button type="button" class="w-full py-2 text-left hover:bg-hover" onclick={() => jumpTo(event.at)}>
									<span class="text-sm font-medium">{event.kind}</span><span class="ml-2 text-xs text-ink-muted">{when(event.at)}</span>
									<pre class="mt-1 whitespace-pre-wrap break-all text-xs text-ink-muted">{JSON.stringify(event.data, null, 2)}</pre>
								</button>
							{/each}
						</div>
						{#if trace.incidents.length > 0}
							<h3 class="mt-5 font-semibold">Incidents</h3>
							{#each trace.incidents as incident (incident.id)}
								<button type="button" class="mt-2 block w-full text-left text-sm text-primary-ink underline" onclick={() => jumpTo(incident.triggered_at)}>{incident.kind} · {incident.reason ?? incident.severity ?? 'No details'} · {when(incident.triggered_at)}</button>
							{/each}
						{/if}
						{#if trace.chute_driver_telemetry.length > 0}
							<details class="mt-5 text-sm"><summary class="font-semibold">Chute driver telemetry near this piece ({trace.chute_driver_telemetry.length} samples)</summary><p class="mt-1 text-ink-muted">Driver diagnostics are time-correlated; they do not measure chute angle.</p><pre class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(trace.chute_driver_telemetry, null, 2)}</pre></details>
						{/if}
					</div>
					<div>
						<h3 class="font-semibold">Brickognize requests</h3>
						{#each trace.requests as request (request.id)}
							<section class="mt-3 rounded-item bg-well p-3">
								<div class="text-sm font-medium">{request.label ?? 'Request'} · HTTP {request.status_code ?? 'No response'} · {when(request.started_at)}</div>
								{#if request.error}<Alert tone="warning">{request.error}</Alert>{/if}
								{#if request.evicted_at}<p class="mt-2 text-sm text-ink-muted">Request images and raw response were removed by the storage limit on {when(request.evicted_at)}.</p>{/if}
								<div class="mt-2 flex flex-wrap gap-2">{#each request.images as requestImage, index}<div><img src={`${activeBase}/api/machine-investigation/requests/${request.id}/images/${index}`} alt="Exact JPEG sent to Brickognize" class="h-28 max-w-40 object-contain" /><div class="text-xs text-ink-muted">{when(requestImage.captured_at)} · channel {requestImage.channel ?? '?'}</div>{#if requestImage.channel === 4 && requestImage.captured_at}<button type="button" class="text-sm text-primary-ink underline" onclick={() => jumpTo(requestImage.captured_at!)}>See source video</button>{/if}</div>{/each}</div>
								<details class="mt-2 text-sm"><summary>Full Brickognize response</summary><pre class="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(request.response, null, 2)}</pre>{#if !request.evicted_at}<a class="text-primary-ink underline" href={`${activeBase}/api/machine-investigation/requests/${request.id}/response`} target="_blank" rel="noreferrer">Open original response body</a>{/if}</details>
							</section>
						{:else}<p class="mt-2 text-sm text-ink-muted">No saved Brickognize request for this piece.</p>{/each}
						<h3 class="mt-5 font-semibold">Stored classification crops</h3>
						<div class="mt-2 flex flex-wrap gap-2">{#each trace.images as image (image.id)}{#if image.available_locally}<img src={`${activeBase}/api/pieces/${encodeURIComponent(pieceQuery)}/images/${image.id}`} alt={`Classification crop at ${when(image.ts)}`} title={when(image.ts)} class="h-24 max-w-32 object-contain" />{/if}{/each}</div>
						<h3 class="mt-5 font-semibold">Other prediction answers</h3>
						{#each trace.predictions as prediction}<div class="mt-2 text-sm"><strong>{prediction.provider} · {prediction.request}</strong><div class="text-ink-muted">{prediction.answers.map((answer) => `${answer.kind}: ${answer.value_id} (${answer.score ?? '?'})${answer.applied ? ' applied' : ''}`).join(' · ')}</div></div>{/each}
					</div>
				</div>
			</Panel>
		{/if}
		<Panel title="Machine event log" description="Motor commands, classification decisions, releases, and recording gaps.">
			<div class="mb-3 flex flex-wrap gap-2"><Input bind:value={runQuery} placeholder="Run ID" class="w-60" /><Input bind:value={kindQuery} placeholder="Event kind" class="w-52" /><Button onclick={loadEvents}>Filter events</Button></div>
			<div class="max-h-96 overflow-y-auto divide-y divide-line">
				{#each events as event (event.id)}
					<div class="flex items-start justify-between gap-3 py-2 text-sm"><div><strong>{event.kind}</strong>{#if event.piece_uuid}<button type="button" class="ml-2 text-primary-ink underline" onclick={() => openPiece(event.piece_uuid!)}>{event.piece_uuid.slice(0, 8)}</button>{/if}<pre class="mt-1 whitespace-pre-wrap break-all text-xs text-ink-muted">{JSON.stringify(event.data)}</pre></div><button type="button" class="num shrink-0 text-primary-ink underline" onclick={() => jumpTo(event.at)}>{when(event.at)}</button></div>
			{:else}<p class="text-sm text-ink-muted">No matching events.</p>{/each}
			</div>
		</Panel>
	</div>
</AppShell>
