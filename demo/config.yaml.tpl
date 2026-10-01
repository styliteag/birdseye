# Combined NetBird server, demo (plain HTTP on netbird.localhost:58080)
server:
  listenAddress: ":80"
  exposedAddress: "http://netbird.localhost:58080"
  stunPorts:
    - 3478
  metricsPort: 9090
  healthcheckAddress: ":9000"
  logLevel: "info"
  logFile: "console"
  authSecret: "@@RELAY_SECRET@@"
  dataDir: "/var/lib/netbird"
  auth:
    issuer: "http://netbird.localhost:58080/oauth2"
    signKeyRefreshEnabled: true
    sessionCookieEncryptionKey: "@@COOKIE_KEY@@"
    dashboardRedirectURIs:
      - "http://netbird.localhost:58080/nb-auth"
      - "http://netbird.localhost:58080/nb-silent-auth"
      - "http://localhost:58090/auth/callback"
    cliRedirectURIs:
      - "http://localhost:53000/"
  store:
    engine: "sqlite"
    encryptionKey: "@@STORE_KEY@@"
