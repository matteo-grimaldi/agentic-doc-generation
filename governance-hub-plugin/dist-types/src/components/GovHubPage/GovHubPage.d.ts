import React from 'react';
/**
 * GovHubPage — embeds the Governance Hub service in a full-page iframe.
 *
 * Access is gated by the `governance-hub.view` permission. Users who are not
 * members of role:default/architects (as configured in rbac-policy.yaml) will
 * see an "Access Restricted" error panel instead of the iframe.
 *
 * The Governance Hub URL is read from Backstage config key `govHub.url`,
 * which is set via the GOVERNANCE_HUB_URL environment variable injected from
 * the rhdh-secrets Secret. The configure-rhdh Job auto-discovers this URL
 * from the governance-hub Route in the doc-gen-demo namespace.
 */
export declare const GovHubPage: () => React.JSX.Element;
