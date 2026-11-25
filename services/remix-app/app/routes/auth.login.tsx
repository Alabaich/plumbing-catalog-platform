import { redirect, type LoaderFunctionArgs } from "react-router";
import { login } from "../shopify.server";
import React from "react";

export const loader = async ({ request }: LoaderFunctionArgs) => {
    await login(request);

    const url = new URL(request.url);
    const returnTo = url.searchParams.get("return_to") || "/app";

    return redirect(returnTo);
};

export default function AuthLogin() {
    return null;
}