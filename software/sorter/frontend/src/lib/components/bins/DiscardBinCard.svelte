<script lang="ts">
	import Badge from '$lib/components/ui/Badge.svelte';
	import Panel from '$lib/components/ui/Panel.svelte';
	import type { BinInfo } from './types';

	let { assignedBin }: { assignedBin?: BinInfo | null } = $props();
</script>

<!-- Discard requires an assigned physical bin; pieces are held if it is unavailable. -->
<Panel title="Discard bin">
	{#snippet actions()}<Badge>{assignedBin ? `Bin ${assignedBin.global_index + 1}` : 'Unassigned'}</Badge>{/snippet}
	{#if assignedBin}
		<p class="text-sm text-ink-muted">
			Unrecognized pieces and recovery sweeps route to bin {assignedBin.global_index + 1}.
			Oversized pieces use it when this machine's discard opening is configured to accept them.
		</p>
	{:else}
		<p class="text-sm text-ink-muted">
			Assign “Discard / unknown” to a physical bin before sorting. Pieces are held when
			no discard bin is available.
		</p>
	{/if}
</Panel>
