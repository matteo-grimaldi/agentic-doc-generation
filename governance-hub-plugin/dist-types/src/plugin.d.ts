/**
 * Permission that gates access to the Governance Hub page.
 * Only users whose RBAC role is granted `governance-hub.view` can see the page.
 * Configured in developer-hub/k8s/instance/rbac-policy.yaml:
 *   p, role:default/architects, governance-hub.view, read, allow
 */
export declare const govHubViewPermission: import("@backstage/plugin-permission-common").BasicPermission;
export declare const govHubPlugin: import("@backstage/core-plugin-api").BackstagePlugin<{
    root: import("@backstage/core-plugin-api").RouteRef<undefined>;
}, {}, {}>;
export declare const GovHubPage: () => import("react").JSX.Element;
