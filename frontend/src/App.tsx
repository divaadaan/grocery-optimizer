import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { UserProvider } from "./features/user/UserContext";
import { HomePage } from "./features/home/HomePage";
import { UserSetupPage } from "./features/user/UserSetupPage";
import { DealsPage } from "./features/deals/DealsPage";
import { PlannerPage } from "./features/planner/PlannerPage";
import { ShoppingListPage } from "./features/shopping/ShoppingListPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, refetchOnWindowFocus: false },
  },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <UserProvider>
        <BrowserRouter>
          <Routes>
            <Route element={<Layout />}>
              <Route path="/" element={<HomePage />} />
              <Route path="/setup" element={<UserSetupPage />} />
              <Route path="/deals" element={<DealsPage />} />
              <Route path="/planner" element={<PlannerPage />} />
              <Route path="/shopping-list" element={<ShoppingListPage />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </UserProvider>
    </QueryClientProvider>
  );
}
