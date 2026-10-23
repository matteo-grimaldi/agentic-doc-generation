import React from 'react';
import { usePermission } from '@backstage/plugin-permission-react';
import { useApi, configApiRef } from '@backstage/core-plugin-api';
import {
  Page,
  Header,
  Content,
  Progress,
  ResponseErrorPanel,
} from '@backstage/core-components';
import { govHubViewPermission } from '../../plugin';

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
export const GovHubPage = () => {
  const { allowed, loading } = usePermission({
    permission: govHubViewPermission,
  });
  const config = useApi(configApiRef);

  if (loading) {
    return <Progress />;
  }

  if (!allowed) {
    return (
      <Page themeId="tool">
        <Header title="Governance Hub" subtitle="Architects only" />
        <Content>
          <ResponseErrorPanel
            error={
              new Error(
                'Access restricted. The Governance Hub is available to members of the architects group only.',
              )
            }
          />
        </Content>
      </Page>
    );
  }

  const govHubUrl = config.getString('govHub.url');

  return (
    <Page themeId="tool">
      <Header
        title="Governance Hub"
        subtitle="AI-powered governance and compliance"
      />
      <Content noPadding>
        <iframe
          src={govHubUrl}
          title="Governance Hub"
          style={{
            width: '100%',
            height: '100%',
            border: 'none',
            minHeight: '85vh',
            display: 'block',
          }}
          allow="clipboard-read; clipboard-write"
        />
      </Content>
    </Page>
  );
};
