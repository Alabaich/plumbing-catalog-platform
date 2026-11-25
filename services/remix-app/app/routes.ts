import { type RouteConfig, index, route } from "@react-router/dev/routes";

export default [
    index("routes/_index.tsx"),
    route("app", "routes/app.tsx", [
        index("routes/app._index.tsx"),
        route("vendors", "routes/app.vendors.tsx"),
    ]),

    route("auth/*", "routes/auth.login.tsx"),
] satisfies RouteConfig;