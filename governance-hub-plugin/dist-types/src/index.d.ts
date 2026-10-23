/**
 * @mcombi/backstage-plugin-governance-hub
 *
 * RHDH dynamic plugin that embeds the Governance Hub service in a full-page
 * iframe, restricted to members of the `architects` RBAC role.
 *
 * Exports:
 *   govHubPlugin          — the Backstage plugin instance
 *   GovHubPage            — the routable page extension (iframe + permission gate)
 *   govHubViewPermission  — the permission used to gate access
 */
export { govHubPlugin, GovHubPage, govHubViewPermission } from './plugin';
