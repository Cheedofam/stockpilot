import axios from "axios";

// Accept VITE_API_URL with or without a trailing /stockpilot —
// all Flask routes live under /stockpilot/api.
const raw = (import.meta.env.VITE_API_URL || "http://localhost:5001")
  .replace(/\/$/, "")
  .replace(/\/stockpilot$/, "");

export const api = axios.create({ baseURL: `${raw}/stockpilot/api` });

export const getStats = () => api.get("/stats").then((r) => r.data);
export const getSignals = () => api.get("/signals").then((r) => r.data);
export const getSignal = (t) => api.get(`/signal/${t}`).then((r) => r.data);
export const getTrades = (limit = 25) =>
  api.get(`/trades?limit=${limit}`).then((r) => r.data);
export const getChart = (t) => api.get(`/chart/${t}`).then((r) => r.data);
export const getNews = (t) => api.get(`/news/${t}`).then((r) => r.data);
export const getWatchlist = () => api.get("/watchlist").then((r) => r.data);
export const addToWatchlist = (t) =>
  api.post(`/watchlist/${t}`).then((r) => r.data);
export const removeFromWatchlist = (t) =>
  api.delete(`/watchlist/${t}`).then((r) => r.data);
