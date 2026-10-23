import {
  createPlugin,
  createRoutableExtension,
} from '@backstage/core-plugin-api';
import { createPermission } from '@backstage/plugin-permission-common';
import { rootRouteRef } from './routes';

/**
 * Permission that gates access to the Governance Hub page.
 * Only users whose RBAC role is granted `governance-hub.view` can see the page.
 * Configured in developer-hub/k8s/instance/rbac-policy.yaml:
 *   p, role:default/architects, governance-hub.view, read, allow
 */
export const govHubViewPermission = createPermission({
  name: 'governance-hub.view',
  attributes: { action: 'read' },
});

export const govHubPlugin = createPlugin({
  id: 'governance-hub',
  routes: {
    root: rootRouteRef,
  },
});

export const GovHubPage = govHubPlugin.provide(
  createRoutableExtension({
    name: 'GovHubPage',
    component: () =>
      import('./components/GovHubPage').then(m => m.GovHubPage),
    mountPoint: rootRouteRef,
  }),
);
