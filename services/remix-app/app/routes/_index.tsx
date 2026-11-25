import { redirect, type LoaderFunctionArgs } from "react-router";

export const loader = ({ request }: LoaderFunctionArgs) => {
    const url = new URL(request.url);
    
    return redirect(`/app${url.search}`);
};

export default function RootIndex() {
    return null;
}