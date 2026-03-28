"use client";

import { useEffect, useState, useCallback } from "react";
import { ProviderConfig } from "./ProviderConfig";
import { ConversationHistory } from "./ConversationHistory";
import { UserMenu } from "./UserMenu";
import { getHealth, getDocuments } from "@/lib/api";
import type { Document } from "@/lib/types";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuItem,
  SidebarMenuButton,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarGroupContent,
  SidebarRail,
  SidebarSeparator,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import {
  FileTextIcon,
  LayoutGridIcon,
  XIcon,
  CheckIcon,
} from "lucide-react";

interface AppSidebarProps extends React.ComponentProps<typeof Sidebar> {
  activeDocIds: string[];
  onToggleDoc: (docId: string) => void;
  onProviderConnected: () => void;
  refreshTrigger: number;
  onDocumentsUploaded: () => void;
  activeConversationId: string | null;
  onSelectConversation: (convId: string) => void;
  onNewConversation: () => void;
  onBrowseCollections: () => void;
}

export function AppSidebar({
  activeDocIds,
  onToggleDoc,
  onProviderConnected,
  refreshTrigger,
  onDocumentsUploaded,
  activeConversationId,
  onSelectConversation,
  onNewConversation,
  onBrowseCollections,
  ...props
}: AppSidebarProps) {
  const [supabaseStatus, setSupabaseStatus] = useState<string | null>(null);
  const [docNames, setDocNames] = useState<Record<string, string>>({});

  useEffect(() => {
    getHealth()
      .then((h) => setSupabaseStatus(h.supabase))
      .catch((e) => {
        console.error("Failed to fetch health:", e);
        setSupabaseStatus("unreachable");
      });
  }, []);

  // Fetch doc names for selected docs
  const fetchDocNames = useCallback(async () => {
    if (activeDocIds.length === 0) return;
    try {
      const docs: Document[] = await getDocuments();
      const names: Record<string, string> = {};
      for (const doc of docs) {
        if (activeDocIds.includes(doc.id)) {
          names[doc.id] = doc.name;
        }
      }
      setDocNames(names);
    } catch {
      // ignore
    }
  }, [activeDocIds]);

  useEffect(() => {
    fetchDocNames();
  }, [fetchDocNames, refreshTrigger]);

  return (
    <Sidebar {...props}>
      <UserMenu />
      <SidebarHeader className="border-b">
        <div className="flex items-center justify-between">
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton size="lg">
                <div className="flex aspect-square size-8 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
                  <FileTextIcon className="size-4" />
                </div>
                <div className="flex flex-col gap-0.5 leading-none">
                  <span className="font-semibold">Doc Agent</span>
                  <span className="text-[11px] text-muted-foreground">Document Q&A</span>
                </div>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
          <SidebarTrigger />
        </div>
      </SidebarHeader>

      <SidebarContent className="px-1">
        <ProviderConfig onConnected={onProviderConnected} />
        <SidebarSeparator />

        {/* Browse Collections button */}
        <SidebarGroup className="p-0">
          <SidebarGroupContent className="px-2 py-1">
            <button
              onClick={onBrowseCollections}
              className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
            >
              <LayoutGridIcon className="size-4" />
              Browse Collections
            </button>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarSeparator />

        {/* Selected Documents summary */}
        <SidebarGroup className="p-0">
          <SidebarGroupLabel className="px-2">
            Selected Documents
            {activeDocIds.length > 0 && (
              <span className="ml-auto rounded-full bg-sidebar-primary px-1.5 text-[10px] text-sidebar-primary-foreground">
                {activeDocIds.length}
              </span>
            )}
          </SidebarGroupLabel>
          <SidebarGroupContent>
            {activeDocIds.length === 0 ? (
              <p className="px-2 py-3 text-center text-[11px] text-muted-foreground">
                No documents selected. Browse a collection to select documents.
              </p>
            ) : (
              <SidebarMenu>
                {activeDocIds.map((docId) => (
                  <SidebarMenuItem key={docId}>
                    <SidebarMenuButton isActive tooltip={docNames[docId] || docId}>
                      <CheckIcon className="size-4 text-primary" />
                      <span className="truncate">{docNames[docId] || docId.slice(0, 8)}</span>
                    </SidebarMenuButton>
                    <button
                      onClick={() => onToggleDoc(docId)}
                      className="absolute right-1 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted-foreground opacity-0 transition-opacity hover:text-foreground [li:hover_&]:opacity-100"
                    >
                      <XIcon className="size-3.5" />
                    </button>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            )}
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarSeparator />
        <ConversationHistory
          activeConversationId={activeConversationId}
          onSelectConversation={onSelectConversation}
          onNewConversation={onNewConversation}
        />
      </SidebarContent>

      <SidebarFooter className="border-t">
        {supabaseStatus && (
          <div className="flex items-center gap-1.5 px-2">
            <span
              className={`inline-block size-2 rounded-full ${
                supabaseStatus === "connected"
                  ? "bg-green-500"
                  : "bg-red-500"
              }`}
            />
            <span className="text-[10px] text-muted-foreground">
              Supabase {supabaseStatus}
            </span>
          </div>
        )}
        <p className="px-2 text-[10px] text-muted-foreground">
          Powered by Doc Agent
        </p>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  );
}
