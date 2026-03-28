"use client";

import { useState, useCallback } from "react";
import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { useChatRuntime, AssistantChatTransport } from "@assistant-ui/react-ai-sdk";
import { Thread } from "@/components/assistant-ui/thread";
import { CollectionCards } from "@/components/CollectionCards";
import { CollectionView } from "@/components/CollectionView";
import { AppSidebar } from "@/components/Sidebar";
import { useAuth } from "@/components/AuthProvider";
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { Separator } from "@/components/ui/separator";
import { LayoutGridIcon } from "lucide-react";

export const Assistant = () => {
  const { user, loading } = useAuth();
  const [activeDocIds, setActiveDocIds] = useState<string[]>([]);
  const [refreshTrigger, setRefreshTrigger] = useState(0);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [activeCollection, setActiveCollection] = useState<string | null>(null);

  const runtime = useChatRuntime({
    transport: new AssistantChatTransport({
      api: "/api/chat",
      body: {
        docIds: activeDocIds,
        conversationId: activeConversationId,
      },
    }),
  });

  const handleToggleDoc = useCallback((docId: string) => {
    setActiveDocIds((prev) =>
      prev.includes(docId) ? prev.filter((id) => id !== docId) : [...prev, docId],
    );
    setActiveConversationId(null);
  }, []);

  const handleProviderConnected = useCallback(() => {}, []);

  const handleDocumentsUploaded = useCallback(() => {
    setRefreshTrigger((t) => t + 1);
  }, []);

  const handleSelectConversation = useCallback((convId: string) => {
    setActiveConversationId(convId);
  }, []);

  const handleNewConversation = useCallback(() => {
    setActiveConversationId(null);
  }, []);

  const handleBackToCollections = useCallback(() => {
    setActiveCollection(null);
  }, []);

  // Determine what to show in the main area
  const showCollectionCards = activeCollection === null && activeDocIds.length === 0;
  const showCollectionView = activeCollection !== null;
  const showChat = activeDocIds.length > 0;

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-zinc-950">
        <div className="text-zinc-400">Loading...</div>
      </div>
    );
  }

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <SidebarProvider defaultOpen={false}>
        <div className="flex h-dvh w-full pr-0.5">
          <AppSidebar
            side="right"
            activeDocIds={activeDocIds}
            onToggleDoc={handleToggleDoc}
            onProviderConnected={handleProviderConnected}
            refreshTrigger={refreshTrigger}
            onDocumentsUploaded={handleDocumentsUploaded}
            activeConversationId={activeConversationId}
            onSelectConversation={handleSelectConversation}
            onNewConversation={handleNewConversation}
            onBrowseCollections={handleBackToCollections}
          />
          <SidebarInset>
            <header className="flex h-14 shrink-0 items-center gap-2 border-b px-4">
              <h1 className="text-sm font-medium">
                {showChat
                  ? `${activeDocIds.length} document${activeDocIds.length > 1 ? "s" : ""} selected`
                  : "Doc Agent"}
              </h1>
              <div className="ml-auto flex items-center gap-2">
                {showChat && (
                  <button
                    onClick={handleBackToCollections}
                    className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                  >
                    <LayoutGridIcon className="size-3.5" />
                    Collections
                  </button>
                )}
                <Separator orientation="vertical" className="h-4" />
                <SidebarTrigger />
              </div>
            </header>

            <div className="flex-1 overflow-y-auto">
              {showCollectionCards && (
                <CollectionCards onSelectCollection={setActiveCollection} />
              )}

              {showCollectionView && (
                <CollectionView
                  collectionId={activeCollection}
                  activeDocIds={activeDocIds}
                  onToggleDoc={handleToggleDoc}
                  onBack={handleBackToCollections}
                  onDocumentsUploaded={handleDocumentsUploaded}
                  userRole={user?.role}
                />
              )}

              {showChat && (
                <div className="h-full">
                  <Thread />
                </div>
              )}
            </div>
          </SidebarInset>
        </div>
      </SidebarProvider>
    </AssistantRuntimeProvider>
  );
};
