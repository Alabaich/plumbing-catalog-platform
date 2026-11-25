import { Outlet, Link, redirect, type LoaderFunctionArgs } from "react-router";
import { authenticate } from "../shopify.server";
import React from "react";

declare global {
    namespace JSX {
        interface IntrinsicElements {
            "ui-nav-menu": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;
        }
    }
}

export const loader = async ({ request }: LoaderFunctionArgs) => {
    try {
        await authenticate.admin(request);
    } catch (error) {
        if (error instanceof Response && error.status === 410) {
            const url = new URL(request.url);
            throw redirect(`/auth/login${url.search}`);
        }
        throw error;
    }

    return null;
};

export default function AppLayout() {
    return (
        <>
            <ui-nav-menu>
                <Link to="/app" rel="home">Home</Link>
                <Link to="/app/vendors">Vendors</Link>
            </ui-nav-menu>
            <Outlet />
        </>
    );
}