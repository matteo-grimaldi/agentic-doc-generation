/**
 * Backstage config schema declaration for the governance-hub plugin.
 *
 * backstage-cli reads this file during `package build` to produce the
 * `govHub` section of dist/.config-schema.json with `visibility: frontend`,
 * which tells Backstage it is safe to forward the value to the browser.
 *
 * Without this file the key is absent from the schema, Backstage strips it
 * during frontend-config filtering, and the plugin throws:
 *   Error: Missing required config value at 'govHub.url' in 'app'
 */
export interface Config {
  /** Governance Hub plugin configuration */
  govHub?: {
    /**
     * URL of the Governance Hub service that is embedded in the RHDH iframe.
     * Injected at runtime via the GOVERNANCE_HUB_URL environment variable from
     * the rhdh-secrets Secret.
     *
     * @visibility frontend
     */
    url: string;
  };
}
