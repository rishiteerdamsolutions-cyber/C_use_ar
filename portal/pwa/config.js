/* Deploy-time overrides: set on window before loading app.js, or replace at build. */
window.CUSEAR_CONFIG = window.CUSEAR_CONFIG || {
  apiBase: window.CUSEAR_API_BASE || "https://api.cusear.autos",
  supabaseUrl: window.CUSEAR_SUPABASE_URL || "",
  supabaseAnonKey: window.CUSEAR_SUPABASE_ANON_KEY || "",
  pricingUrl: "/pricing.html",
  siteUrl: "/",
};
